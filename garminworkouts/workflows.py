import json
import os
import secrets
from datetime import date, timedelta
from pathlib import Path

from garminworkouts.activities import ActivityArchive, ActivitySummary, AssessmentSelector
from garminworkouts.fit_analysis import FitAnalyzer
from garminworkouts.garmin.garminclient import GarminClient
from garminworkouts.garmin.ratelimit import GarminRateLimiter, GarminRateLimitError, has_reusable_tokens
from garminworkouts.llm import LLMConfig, create_advisor
from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.plan import PlanApplier
from garminworkouts.planner import DeterministicPlanner, write_plan
from garminworkouts.replanning import calendar_changes, immutable_workout_keys, validate_mutable_replacement
from garminworkouts.retire import PlanRetirement, ScheduledConflictCleanup


class GarminAuthenticationRequiredError(ValueError):
    pass


class PlannerWorkflow:
    """Non-interactive application workflows shared by web and future clients."""

    def __init__(self, state, today=None):
        self.state = state
        self.today = today or date.today()
        self.planner = DeterministicPlanner()

    def garmin_client(self, password=None):
        username = self.state.get_setting("garmin_username", os.getenv("GARMIN_USERNAME"))
        if not username:
            raise GarminAuthenticationRequiredError("Connect a Garmin account first")
        token_store = Path(self.state.get_setting("garmin_token_store", str(self.state.tokens_dir))).expanduser()
        rate_limiter = GarminRateLimiter(token_store)
        rate_limiter.check_cooldown()
        password = password or os.getenv("GARMIN_PASSWORD")
        if not password and not has_reusable_tokens(token_store):
            raise GarminAuthenticationRequiredError(
                "The Garmin session has no reusable tokens; reconnect with the Garmin password"
            )
        return GarminClient(
            username=username,
            password=password,
            token_store=str(token_store),
            rate_limiter=rate_limiter,
        )

    def connect_garmin(self, username, password):
        username = str(username or "").strip()
        password = str(password or "")
        if not username or not password:
            raise ValueError("Both Garmin username and password are required")
        rate_limiter = GarminRateLimiter(self.state.tokens_dir)
        rate_limiter.check_cooldown()
        with GarminClient(
            username,
            password,
            str(self.state.tokens_dir),
            rate_limiter=rate_limiter,
        ) as connection:
            connection.list_recent_activities(1)
        self.state.set_setting("garmin_username", username)
        self.state.set_setting("garmin_token_store", str(self.state.tokens_dir))
        self.state.record_event("garmin-connected-from-web", {"username": username})

    def fetch_activities(self, start_date, end_date, connection=None):
        if connection is None:
            with self.garmin_client() as managed_connection:
                raw = managed_connection.list_activities_by_date(
                    start_date.isoformat(), end_date.isoformat(), "running"
                )
        else:
            raw = connection.list_activities_by_date(start_date.isoformat(), end_date.isoformat(), "running")
        return sorted((ActivitySummary.from_garmin(item) for item in raw), key=lambda item: item.started_at)

    def fetch_recent_history(self, connection=None):
        return self.fetch_activities(self.today - timedelta(days=42), self.today, connection=connection)

    def refresh(self, connection=None):
        plan = self.state.active_plan()
        if not plan:
            raise ValueError("There is no active plan to refresh")
        if connection is None:
            with self.garmin_client() as managed_connection:
                return self.refresh(connection=managed_connection)
        activities = self.fetch_activities(
            plan.start_date - timedelta(days=1),
            self.today + timedelta(days=1),
            connection=connection,
        )
        progress_before_refresh = self.state.progress(plan.id)
        checked_activity_ids = {
            row["activity_id"]
            for row in progress_before_refresh
            if row["activity_id"] and row["execution_score_checked_at"]
        }
        planned_dates = {date.fromisoformat(str(workout["date"])) for workout in plan.config.get("workouts", [])}
        activities, score_errors = FitAnalyzer().enrich_execution_scores(
            activities,
            planned_dates,
            checked_activity_ids,
            connection,
        )
        progress = self.state.refresh_progress(plan.id, activities, today=self.today)
        self.state.record_event(
            "progress-refreshed",
            {
                "plan_id": plan.id,
                "activity_count": len(activities),
                "execution_score_errors": score_errors,
            },
        )
        return progress

    def generate_plan(self):
        goal = self.state.active_goal()
        if not goal:
            raise ValueError("Create a goal before generating a plan")
        if self.state.active_plan():
            raise ValueError("An active plan already exists; reassess its remaining workouts instead")
        activities = self.fetch_recent_history()
        proposal = self.planner.generate(goal, activities)
        return self._save_proposal(proposal, self.state.active_plan())

    def adapt_plan(self):
        goal = self.state.active_goal()
        active_plan = self.state.active_plan()
        if not goal or not active_plan:
            raise ValueError("An active goal and plan are required")
        with self.garmin_client() as connection:
            self.refresh(connection=connection)
            progress_rows = self.state.block_progress(active_plan.id)
            progress = self.state.block_progress_summary(active_plan.id)
            if progress["completed"] < 2:
                raise ValueError(
                    "Fewer than two scheduled runs are complete; retaining the current plan is recommended"
                )
            activities = self.fetch_recent_history(connection=connection)
            fit_analysis = self._prepare_fit_analysis(activities, connection)
        proposal = self.planner.adapt_remaining(
            goal,
            active_plan,
            activities,
            today=self.today,
            fit_analysis=fit_analysis,
            progress=progress_rows,
        )
        changes = self.calendar_changes(
            active_plan.config,
            proposal.config,
            today=self.today,
            progress=progress_rows,
        )
        if not changes:
            self.state.record_event("adaptation-retained", {"plan_id": active_plan.id})
            return None, []
        return self._save_proposal(proposal, active_plan), changes

    def inspect_plan_conflicts(self, record):
        self._validate_replacement(record)
        plan = TrainingPlan(record.config)
        with self.garmin_client() as connection:
            preview = ScheduledConflictCleanup(plan, connection, today=self.today).preview()
        self._write_private_json(self._plan_conflict_path(record.id), preview)
        return preview

    def load_plan_conflicts(self, plan_id):
        return self._read_json(self._plan_conflict_path(plan_id))

    def apply_plan(self, record, preview, remove_conflicts, delete_templates, allow_duplicates):
        self._validate_replacement(record)
        self._validate_conflict_choice(preview, remove_conflicts, delete_templates, allow_duplicates)
        new_plan = TrainingPlan(record.config)
        old_record = self.state.plan(record.supersedes_plan_id) if record.supersedes_plan_id else None
        changes_started = False
        try:
            with self.garmin_client() as connection:
                self._verify_conflict_preview(new_plan, connection, preview)
                changes_started = True
                apply_actions = PlanApplier(new_plan, connection).apply(schedule=True)
                conflict_actions = []
                if remove_conflicts:
                    cleanup = ScheduledConflictCleanup(new_plan, connection, today=self.today)
                    conflict_actions = cleanup.apply(preview, delete_templates=delete_templates)
                retirement_actions = []
                if old_record:
                    old_plan = TrainingPlan(old_record.config)
                    retirement = PlanRetirement(
                        old_plan,
                        connection,
                        protected_plans=[new_plan],
                        today=self.today,
                        immutable_workouts=immutable_workout_keys(self.state.progress(old_record.id)),
                    )
                    retirement_actions = retirement.apply(retirement.preview())
        except GarminRateLimitError:
            raise
        except Exception as exc:
            if changes_started:
                self.state.mark_plan_failed(record.id, exc)
            raise

        self.state.activate_plan(record.id)
        self.state.record_event(
            "plan-applied-from-web",
            {
                "plan_id": record.id,
                "superseded_plan_id": old_record.id if old_record else None,
                "apply_actions": apply_actions,
                "conflict_cleanup_actions": conflict_actions,
                "retirement_actions": retirement_actions,
            },
        )
        return {
            "apply": apply_actions,
            "conflicts": conflict_actions,
            "retirement": retirement_actions,
        }

    def explain_plan(self, record, api_key=None):
        config = LLMConfig.from_state(self.state)
        if not config.enabled:
            raise ValueError("Enable an optional LLM explanation provider in Settings first")
        api_key = api_key or os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(f"Enter an API key in Settings or set {config.api_key_env} in the service environment")
        old_record = self.state.plan(record.supersedes_plan_id) if record.supersedes_plan_id else None
        assessment = {
            "goal": record.config.get("metadata", {}).get("goal", {}),
            "baseline": record.config.get("metadata", {}).get("baseline", {}),
            "confidence": record.confidence,
            "rationale": list(record.rationale),
            "calendar_changes": (
                self.calendar_changes(
                    old_record.config,
                    record.config,
                    today=self.today,
                    progress=self.state.progress(old_record.id),
                )
                if old_record
                else []
            ),
        }
        explanation = create_advisor(config, api_key).explain(assessment)
        self._write_private_json(self._plan_explanation_path(record.id), {"explanation": explanation})
        self.state.record_event(
            "plan-explained-from-web", {"plan_id": record.id, "provider": config.provider, "model": config.model}
        )
        return explanation

    def load_plan_explanation(self, plan_id):
        return self._read_json(self._plan_explanation_path(plan_id))["explanation"]

    def inspect_active_conflicts(self):
        record = self.state.active_plan()
        if not record:
            raise ValueError("There is no active plan to protect during cleanup")
        plan = TrainingPlan(record.config)
        with self.garmin_client() as connection:
            preview = ScheduledConflictCleanup(plan, connection, today=self.today).preview()
        self._write_private_json(self._cleanup_conflict_path(record.id), preview)
        return record, preview

    def load_active_conflicts(self, plan_id):
        return self._read_json(self._cleanup_conflict_path(plan_id))

    def clean_active_conflicts(self, record, preview, delete_templates=False):
        unresolved = preview["summary"]["unresolved_calendar_entries"]
        if unresolved:
            raise ValueError("One or more Garmin entries lack a schedule ID; no changes were made")
        plan = TrainingPlan(record.config)
        with self.garmin_client() as connection:
            self._verify_conflict_preview(plan, connection, preview)
            actions = ScheduledConflictCleanup(plan, connection, today=self.today).apply(
                preview,
                delete_templates=delete_templates,
            )
        self.state.record_event(
            "schedule-overlaps-cleaned-from-web",
            {"active_plan_id": record.id, "actions": actions},
        )
        return actions

    def save_one_off_draft(self, config):
        TrainingPlan(config)
        draft_id = secrets.token_urlsafe(18)
        self._write_private_json(self._draft_path(draft_id), config)
        return draft_id

    def load_one_off_draft(self, draft_id):
        self._validate_draft_id(draft_id)
        config = self._read_json(self._draft_path(draft_id))
        TrainingPlan(config)
        return config

    def inspect_one_off_conflicts(self, draft_id):
        plan = TrainingPlan(self.load_one_off_draft(draft_id))
        with self.garmin_client() as connection:
            preview = ScheduledConflictCleanup(plan, connection, today=self.today).preview()
        self._write_private_json(self._draft_conflict_path(draft_id), preview)
        return preview

    def load_one_off_conflicts(self, draft_id):
        self._validate_draft_id(draft_id)
        return self._read_json(self._draft_conflict_path(draft_id))

    def apply_one_off(self, draft_id, preview, remove_conflicts, delete_templates, allow_duplicates):
        self._validate_conflict_choice(preview, remove_conflicts, delete_templates, allow_duplicates)
        config = self.load_one_off_draft(draft_id)
        plan = TrainingPlan(config)
        with self.garmin_client() as connection:
            self._verify_conflict_preview(plan, connection, preview)
            apply_actions = PlanApplier(plan, connection).apply(schedule=True)
            conflict_actions = []
            if remove_conflicts:
                conflict_actions = ScheduledConflictCleanup(plan, connection, today=self.today).apply(
                    preview,
                    delete_templates=delete_templates,
                )
        workout = config["workouts"][0]
        self.state.record_event(
            "one-off-heart-rate-workout-applied-from-web",
            {
                "date": workout["date"],
                "name": workout["name"],
                "apply_actions": apply_actions,
                "conflict_cleanup_actions": conflict_actions,
            },
        )
        return {"apply": apply_actions, "conflicts": conflict_actions}

    def _save_proposal(self, proposal, old_record):
        goal = self.state.active_goal()
        path = self._plan_path(proposal.config)
        write_plan(path, proposal.config)
        return self.state.save_plan(
            goal,
            proposal.config,
            path,
            proposal.confidence,
            proposal.rationale,
            supersedes_plan_id=old_record.id if old_record else None,
        )

    def _validate_replacement(self, record):
        old_record = self.state.plan(record.supersedes_plan_id) if record.supersedes_plan_id else None
        validate_mutable_replacement(
            old_record.config if old_record else {"workouts": []},
            record.config,
            today=self.today,
            progress=self.state.progress(old_record.id) if old_record else None,
        )

    def _prepare_fit_analysis(self, activities, connection):
        try:
            selection = AssessmentSelector().select(activities)
            if not selection.activities:
                raise ValueError("No activities are available for FIT assessment")
            destination = self.state.activities_dir / f"assessment-{selection.coverage_end.isoformat()}"
            ActivityArchive(destination).prepare(selection, connection)
            analysis = FitAnalyzer().analyze_manifest(destination / "manifest.json")
        except GarminRateLimitError:
            raise
        except Exception as exc:
            return {"activities": [], "failures": [{"error": str(exc)}]}
        self.state.record_event(
            "fit-assessment-created",
            {
                "decoded": len(analysis.get("activities", [])),
                "failures": len(analysis.get("failures", [])),
            },
        )
        return analysis

    def _plan_path(self, config):
        start = config["workouts"][0]["date"]
        slug = config["name"].lower().replace(" ", "-").replace("_", "-")
        slug = "".join(character for character in slug if character.isalnum() or character == "-")[:60]
        return self.state.plans_dir / f"{start}-{slug}.yaml"

    @staticmethod
    def calendar_changes(old_config, new_config, today=None, progress=None):
        return calendar_changes(old_config, new_config, today=today, progress=progress)

    @staticmethod
    def _validate_conflict_choice(preview, remove_conflicts, delete_templates, allow_duplicates):
        count = preview["summary"]["overlapping_calendar_entries"]
        unresolved = preview["summary"]["unresolved_calendar_entries"]
        if remove_conflicts and unresolved:
            raise ValueError("One or more Garmin entries lack a schedule ID; no changes were made")
        if delete_templates and not remove_conflicts:
            raise ValueError("Workout templates can only be deleted with their conflicting calendar entries")
        if count and not remove_conflicts and not allow_duplicates:
            raise ValueError("Explicitly remove the conflicts or acknowledge that duplicate dates may remain")

    def _verify_conflict_preview(self, plan, connection, expected):
        current = ScheduledConflictCleanup(plan, connection, today=self.today).preview()
        if current != expected:
            raise ValueError("The Garmin schedule changed after inspection. Inspect it again before applying.")

    @property
    def _web_state_dir(self):
        path = self.state.data_dir / "web"
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path

    def _plan_conflict_path(self, plan_id):
        return self._web_state_dir / f"plan-{int(plan_id)}-conflicts.json"

    def _cleanup_conflict_path(self, plan_id):
        return self._web_state_dir / f"cleanup-{int(plan_id)}-conflicts.json"

    def _plan_explanation_path(self, plan_id):
        return self._web_state_dir / f"plan-{int(plan_id)}-explanation.json"

    def _draft_path(self, draft_id):
        self._validate_draft_id(draft_id)
        return self._web_state_dir / f"draft-{draft_id}.json"

    def _draft_conflict_path(self, draft_id):
        self._validate_draft_id(draft_id)
        return self._web_state_dir / f"draft-{draft_id}-conflicts.json"

    @staticmethod
    def _validate_draft_id(draft_id):
        if not draft_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in draft_id
        ):
            raise ValueError("Invalid draft identifier")

    @staticmethod
    def _write_private_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2))
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)

    @staticmethod
    def _read_json(path):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise ValueError("Inspect Garmin schedule conflicts before applying this item") from exc
