import json
import os
import secrets
from datetime import date, timedelta
from pathlib import Path

from garminworkouts.activities import ActivityArchive, ActivitySummary, AssessmentSelector
from garminworkouts.fit_analysis import FitAnalyzer
from garminworkouts.garmin.garminclient import GarminClient
from garminworkouts.garmin.official_training import OfficialObjectRegistry, OfficialTrainingPublisher
from garminworkouts.garmin.personalclient import PersonalGarminClient
from garminworkouts.garmin.provider import CURRENT_ACTIVITY_REFRESH_CAPABILITIES, require_connection_capabilities
from garminworkouts.garmin.ratelimit import GarminRateLimiter, GarminRateLimitError, has_reusable_tokens
from garminworkouts.garmin.starterclient import GarminStarterNotFoundError, StarterGarminClient
from garminworkouts.llm import LLMConfig, create_advisor
from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.plan import PlanApplier
from garminworkouts.planner import DeterministicPlanner, write_plan
from garminworkouts.replanning import calendar_changes, immutable_workout_keys, validate_mutable_replacement
from garminworkouts.retire import (
    PlanRetirement,
    ScheduledConflictCleanup,
    SelectablePlanScheduleCleanup,
    SelectableWorkoutLibraryCleanup,
    combined_training_plan,
)


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

    def activity_provider_key(self):
        configured = self.state.get_setting(
            "garmin_activity_provider",
            os.getenv("GARMIN_ACTIVITY_PROVIDER", "legacy"),
        )
        provider = str(configured or "legacy").strip().casefold()
        if provider not in {"legacy", "personal"}:
            raise ValueError("Garmin activity provider must be 'legacy' or 'personal'")
        return provider

    def personal_api_configured(self):
        key = str(os.getenv("GARMIN_PERSONAL_API_KEY", "")).strip()
        return bool(key and key.casefold() not in {"unset", "none"})

    def activity_client(self):
        if self.activity_provider_key() == "legacy":
            return self.garmin_client()
        if not self.personal_api_configured():
            raise GarminAuthenticationRequiredError(
                "The official Personal API is selected, but GARMIN_PERSONAL_API_KEY is not configured"
            )
        token_store = self.state.data_dir / "personal-api"
        rate_limiter = GarminRateLimiter(token_store)
        rate_limiter.check_cooldown()
        return PersonalGarminClient(
            os.getenv("GARMIN_PERSONAL_API_KEY"),
            token_store,
            rate_limiter=rate_limiter,
        )

    def training_provider_key(self):
        configured = self.state.get_setting(
            "garmin_training_provider",
            os.getenv("GARMIN_TRAINING_PROVIDER", "legacy"),
        )
        provider = str(configured or "legacy").strip().casefold()
        if provider not in {"legacy", "starter"}:
            raise ValueError("Garmin training provider must be 'legacy' or 'starter'")
        return provider

    def training_client(self):
        if self.training_provider_key() == "legacy":
            return self.garmin_client()
        if not self.starter_connected():
            raise GarminAuthenticationRequiredError("Connect the official Garmin Starter API account first")
        permissions = self._starter_permissions()
        if "WORKOUT_IMPORT" not in permissions:
            raise GarminAuthenticationRequiredError(
                "The connected official Garmin account has not granted WORKOUT_IMPORT permission"
            )
        return self.starter_client()

    def select_training_provider(self, provider):
        provider = str(provider or "").strip().casefold()
        if provider not in {"legacy", "starter"}:
            raise ValueError("Choose either the legacy Connect session or official Starter API")
        if provider == "starter":
            if not self.starter_connected():
                raise GarminAuthenticationRequiredError("Connect the official Garmin Starter API account first")
            if "WORKOUT_IMPORT" not in self._starter_permissions():
                raise GarminAuthenticationRequiredError(
                    "The connected official Garmin account has not granted WORKOUT_IMPORT permission"
                )
        self.state.set_setting("garmin_training_provider", provider)
        self.state.record_event("garmin-training-provider-selected", {"provider": provider})

    @staticmethod
    def starter_api_configured():
        return all(
            str(os.getenv(name, "")).strip()
            for name in (
                "GARMIN_STARTER_CLIENT_ID",
                "GARMIN_STARTER_CLIENT_SECRET",
                "GARMIN_STARTER_REDIRECT_URI",
            )
        )

    def starter_client(self):
        if not self.starter_api_configured():
            raise GarminAuthenticationRequiredError(
                "Configure GARMIN_STARTER_CLIENT_ID, GARMIN_STARTER_CLIENT_SECRET, and "
                "GARMIN_STARTER_REDIRECT_URI first"
            )
        token_store = self.state.data_dir / "starter-api"
        return StarterGarminClient(
            os.getenv("GARMIN_STARTER_CLIENT_ID"),
            os.getenv("GARMIN_STARTER_CLIENT_SECRET"),
            os.getenv("GARMIN_STARTER_REDIRECT_URI"),
            token_store,
            rate_limiter=GarminRateLimiter(token_store),
        )

    def starter_connected(self):
        if not self.starter_api_configured():
            return False
        return self.starter_client().is_connected()

    def begin_starter_authorization(self):
        return self.starter_client().authorization()

    def complete_starter_authorization(self, code, code_verifier):
        client = self.starter_client()
        client.exchange_code(code, code_verifier)
        status = client.connection_status()
        permissions = set(status["permissions"])
        self.state.set_setting("garmin_starter_user_id", status["user_id"])
        self.state.set_setting("garmin_starter_permissions", json.dumps(sorted(permissions)))
        self.state.record_event(
            "garmin-starter-connected",
            {"user_id": status["user_id"], "permissions": sorted(permissions)},
        )
        return status

    def disconnect_starter(self):
        self.starter_client().disconnect()
        self.state.set_setting("garmin_starter_user_id", "")
        self.state.set_setting("garmin_starter_permissions", "[]")
        if self.training_provider_key() == "starter":
            self.state.set_setting("garmin_training_provider", "legacy")
        self.state.record_event("garmin-starter-disconnected", {})

    def _starter_permissions(self):
        try:
            value = json.loads(self.state.get_setting("garmin_starter_permissions", "[]"))
        except (TypeError, ValueError):
            return set()
        return {str(item) for item in value} if isinstance(value, list) else set()

    def _official_registry(self):
        return OfficialObjectRegistry(
            self.state.data_dir / "starter-api",
            self.state.get_setting("garmin_starter_user_id", ""),
        )

    def _official_publisher(self, plan, connection, plan_id):
        return OfficialTrainingPublisher(
            plan,
            connection,
            self._official_registry(),
            plan_id,
            today=self.today,
        )

    def select_activity_provider(self, provider, test=True):
        provider = str(provider or "").strip().casefold()
        if provider not in {"legacy", "personal"}:
            raise ValueError("Choose either the legacy Connect session or official Personal API")
        previous = self.state.get_setting("garmin_activity_provider")
        self.state.set_setting("garmin_activity_provider", provider)
        try:
            if test:
                with self.activity_client() as connection:
                    connection.list_recent_activities(1)
        except Exception:
            if previous is None:
                self.state.set_setting("garmin_activity_provider", "legacy")
            else:
                self.state.set_setting("garmin_activity_provider", previous)
            raise
        self.state.record_event("garmin-activity-provider-selected", {"provider": provider})

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
        self._record_garmin_connection(username)

    def begin_garmin_login(self, username, password):
        """Start a web login and return a live client only when Garmin requests MFA."""
        username = str(username or "").strip()
        password = str(password or "")
        if not username or not password:
            raise ValueError("Both Garmin username and password are required")
        rate_limiter = GarminRateLimiter(self.state.tokens_dir)
        rate_limiter.check_cooldown()
        connection = GarminClient(
            username,
            password,
            str(self.state.tokens_dir),
            rate_limiter=rate_limiter,
            defer_mfa=True,
        )
        try:
            connection.open()
            if connection.mfa_required:
                return connection
            connection.list_recent_activities(1)
        except Exception:
            connection.close()
            raise
        connection.close()
        self._record_garmin_connection(username)
        return None

    def complete_garmin_login(self, username, connection, code):
        """Complete a pending web MFA challenge and verify the resulting session."""
        try:
            connection.resume_mfa(code)
            connection.list_recent_activities(1)
        finally:
            if not connection.mfa_required:
                connection.close()
        self._record_garmin_connection(username)

    def _record_garmin_connection(self, username):
        self.state.set_setting("garmin_username", username)
        self.state.set_setting("garmin_token_store", str(self.state.tokens_dir))
        self.state.record_event("garmin-connected-from-web", {"username": username})

    def fetch_activities(self, start_date, end_date, connection=None):
        if connection is None:
            with self.activity_client() as managed_connection:
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
            with self.activity_client() as managed_connection:
                return self.refresh(connection=managed_connection)
        require_connection_capabilities(
            connection,
            "refresh activities and FIT evidence",
            *CURRENT_ACTIVITY_REFRESH_CAPABILITIES,
        )
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

    def generate_next_block(self):
        goal = self.state.active_goal()
        active_plan = self.state.active_plan()
        if not goal or not active_plan:
            raise ValueError("An active goal and finished plan are required")

        with self.activity_client() as connection:
            self.refresh(connection=connection)
            progress_rows = self.state.block_progress(active_plan.id)
            activities = self.fetch_recent_history(connection=connection)
            fit_analysis = self._prepare_fit_analysis(activities, connection)

        proposal = self.planner.generate_next_block(
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
        record = self._save_proposal(proposal, active_plan)
        self.state.record_event("next-block-proposed", {"plan_id": record.id, "previous_plan_id": active_plan.id})
        return record, changes

    def adapt_plan(self):
        goal = self.state.active_goal()
        active_plan = self.state.active_plan()
        if not goal or not active_plan:
            raise ValueError("An active goal and plan are required")
        with self.activity_client() as connection:
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
        with self.training_client() as connection:
            if self.training_provider_key() == "starter":
                preview = self._official_publisher(plan, connection, record.id).preview_conflicts()
            else:
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
            with self.training_client() as connection:
                official = self.training_provider_key() == "starter"
                publisher = self._official_publisher(new_plan, connection, record.id) if official else None
                self._verify_conflict_preview(new_plan, connection, preview, plan_id=record.id)
                changes_started = True
                apply_actions = (
                    publisher.apply(schedule=True)
                    if publisher
                    else PlanApplier(new_plan, connection).apply(schedule=True)
                )
                conflict_actions = []
                if remove_conflicts:
                    if publisher:
                        conflict_actions = publisher.apply_conflicts(preview, delete_templates=delete_templates)
                    else:
                        cleanup = ScheduledConflictCleanup(new_plan, connection, today=self.today)
                        conflict_actions = cleanup.apply(preview, delete_templates=delete_templates)
                retirement_actions = []
                if old_record:
                    old_plan_finished = self.plan_is_finished(old_record)
                    old_plan = (
                        combined_training_plan(
                            self.state.block_lineage(old_record.id),
                            name=old_record.name,
                        )
                        if old_plan_finished
                        else TrainingPlan(old_record.config)
                    )
                    if publisher:
                        old_plan_ids = [item.id for item in self.state.block_lineage(old_record.id)]
                        retirement_actions = publisher.retire(
                            old_plan,
                            old_plan_ids,
                            protected_plan=new_plan,
                            delete_finished_templates=old_plan_finished,
                        )
                    else:
                        retirement = PlanRetirement(
                            old_plan,
                            connection,
                            protected_plans=[new_plan],
                            today=self.today,
                            immutable_workouts=immutable_workout_keys(self.state.progress(old_record.id)),
                            delete_finished_templates=old_plan_finished,
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
        with self.training_client() as connection:
            if self.training_provider_key() == "starter":
                preview = self._official_publisher(plan, connection, record.id).preview_conflicts()
            else:
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
        with self.training_client() as connection:
            self._verify_conflict_preview(plan, connection, preview, plan_id=record.id)
            if self.training_provider_key() == "starter":
                actions = self._official_publisher(plan, connection, record.id).apply_conflicts(
                    preview, delete_templates=delete_templates
                )
            else:
                actions = ScheduledConflictCleanup(plan, connection, today=self.today).apply(
                    preview,
                    delete_templates=delete_templates,
                )
        self.state.record_event(
            "schedule-overlaps-cleaned-from-web",
            {"active_plan_id": record.id, "actions": actions},
        )
        return actions

    def inspect_selectable_schedules(self):
        record = self.state.active_plan()
        if not record:
            raise ValueError("There is no active plan to inspect")
        plan = combined_training_plan(self.state.block_lineage(record.id), name=record.name)
        with self.training_client() as connection:
            if self.training_provider_key() == "starter":
                preview = self._official_publisher(plan, connection, record.id).preview_plan_schedules()
            else:
                preview = SelectablePlanScheduleCleanup(plan, connection, today=self.today).preview()
        self._write_private_json(self._selectable_schedules_path(record.id), preview)
        return record, preview

    def load_selectable_schedules(self, plan_id):
        return self._read_json(self._selectable_schedules_path(plan_id))

    def remove_selected_schedules(self, record, expected, selected_schedule_ids):
        current = self.state.active_plan()
        if current is None or current.id != record.id:
            raise ValueError("The active plan changed after inspection. Inspect the schedule again.")
        plan_records = self.state.block_lineage(current.id)
        plan = combined_training_plan(plan_records, name=current.name)
        with self.training_client() as connection:
            if self.training_provider_key() == "starter":
                cleanup = self._official_publisher(plan, connection, current.id)
                preview = cleanup.preview_plan_schedules()
            else:
                cleanup = SelectablePlanScheduleCleanup(plan, connection, today=self.today)
                preview = cleanup.preview()
            if preview != expected:
                raise ValueError("The Garmin schedule changed after inspection. Inspect it again before applying.")
            actions = (
                cleanup.apply_selected_schedules(preview, selected_schedule_ids)
                if self.training_provider_key() == "starter"
                else cleanup.apply(preview, selected_schedule_ids)
            )
        removed_keys = {(item["date"], item["name"]) for item in actions}
        skipped = self.state.mark_workouts_skipped(
            [item.id for item in plan_records],
            removed_keys,
            today=self.today,
        )
        self.state.record_event(
            "selected-schedules-removed-from-web",
            {
                "active_plan_id": current.id,
                "actions": actions,
                "future_workouts_marked_skipped": skipped,
            },
        )
        return actions

    def inspect_workout_library(self):
        preview = self._current_workout_library_preview()
        self._write_private_json(self._workout_library_path(), preview)
        return preview

    def load_workout_library(self):
        return self._read_json(self._workout_library_path())

    def delete_selected_workouts(self, expected, selected_workout_ids):
        current = self._current_workout_library_preview()
        if current != expected:
            raise ValueError("The Garmin workout library changed after inspection. Inspect it again before applying.")
        selected = {str(value) for value in selected_workout_ids if str(value).strip()}
        if not selected:
            raise ValueError("Select at least one Garmin workout template to delete")
        items_by_id = {str(item["workout_id"]): item for item in current["workouts"]}
        if selected - set(items_by_id):
            raise ValueError("The selected Garmin workout templates were not part of the inspected library")

        official_ids = {workout_id for workout_id in selected if items_by_id[workout_id]["source"] == "official"}
        legacy_ids = selected - official_ids
        actions = []
        if official_ids and (not self.starter_connected() or "WORKOUT_IMPORT" not in self._starter_permissions()):
            raise GarminAuthenticationRequiredError(
                "Reconnect the official Garmin Starter API before deleting its workout templates"
            )
        if legacy_ids:
            with self.garmin_client() as connection:
                actions.extend(SelectableWorkoutLibraryCleanup(connection).apply(current, legacy_ids))
        if official_ids:
            registry = self._official_registry()
            with self.starter_client() as connection:
                for workout_id in official_ids:
                    item = items_by_id[workout_id]
                    try:
                        connection.delete_workout(item["workout_id"])
                    except GarminStarterNotFoundError:
                        pass
                    registry.forget_workout(workout_id=item["workout_id"])
                    actions.append(
                        {
                            "action": "deleted-selected-workout-template",
                            "name": item["name"],
                            "workout_id": item["workout_id"],
                            "source": "official",
                        }
                    )
        self.state.record_event("selected-workout-templates-deleted-from-web", {"actions": actions})
        return actions

    def _current_workout_library_preview(self):
        with self.garmin_client() as connection:
            preview = SelectableWorkoutLibraryCleanup(connection).preview()
        items_by_id = {str(item["workout_id"]): item for item in preview["workouts"]}
        user_id = str(self.state.get_setting("garmin_starter_user_id", "")).strip()
        if user_id:
            registry = self._official_registry()
            for recorded in registry.workouts():
                workout_id = str(recorded["workout_id"])
                item = items_by_id.setdefault(
                    workout_id,
                    {
                        "name": recorded["name"],
                        "workout_id": recorded["workout_id"],
                        "action": "delete",
                    },
                )
                item["source"] = "official"
        preview["workouts"] = sorted(
            items_by_id.values(),
            key=lambda item: (item["name"].casefold(), str(item["workout_id"])),
        )
        preview["summary"]["selectable_workout_templates"] = len(preview["workouts"])
        return preview

    def inspect_previous_block_templates(self):
        record, previous = self._finished_previous_block()
        active_plan = combined_training_plan(self.state.block_lineage(record.id), name=record.name)
        previous_plan = combined_training_plan(self.state.block_lineage(previous.id), name=previous.name)
        with self.training_client() as connection:
            if self.training_provider_key() == "starter":
                preview = self._official_publisher(active_plan, connection, record.id).preview_retirement_templates(
                    previous_plan, protected_plan=active_plan
                )
            else:
                preview = PlanRetirement(
                    previous_plan,
                    connection,
                    protected_plans=[active_plan],
                    today=self.today,
                    immutable_workouts=immutable_workout_keys(self.state.progress(previous.id)),
                    delete_finished_templates=True,
                ).preview()
        self._write_private_json(self._obsolete_cleanup_path(record.id), preview)
        return record, previous, preview

    def load_previous_block_templates(self, plan_id):
        return self._read_json(self._obsolete_cleanup_path(plan_id))

    def clean_previous_block_templates(self, record, expected):
        current, previous = self._finished_previous_block()
        if current.id != record.id:
            raise ValueError("The active plan changed after inspection. Inspect the cleanup again.")
        active_plan = combined_training_plan(self.state.block_lineage(current.id), name=current.name)
        previous_plan = combined_training_plan(self.state.block_lineage(previous.id), name=previous.name)
        with self.training_client() as connection:
            if self.training_provider_key() == "starter":
                retirement = self._official_publisher(active_plan, connection, current.id)
                preview = retirement.preview_retirement_templates(previous_plan, protected_plan=active_plan)
            else:
                retirement = PlanRetirement(
                    previous_plan,
                    connection,
                    protected_plans=[active_plan],
                    today=self.today,
                    immutable_workouts=immutable_workout_keys(self.state.progress(previous.id)),
                    delete_finished_templates=True,
                )
                preview = retirement.preview()
            if preview != expected:
                raise ValueError("The Garmin workout library changed after inspection. Inspect the cleanup again.")
            actions = (
                retirement.apply_retirement_templates(preview)
                if self.training_provider_key() == "starter"
                else retirement.apply_templates(preview)
            )
        self.state.record_event(
            "finished-block-templates-cleaned-from-web",
            {"active_plan_id": current.id, "previous_plan_id": previous.id, "actions": actions},
        )
        return actions

    def finished_previous_block(self):
        try:
            return self._finished_previous_block()[1]
        except ValueError:
            return None

    def _finished_previous_block(self):
        record = self.state.active_plan()
        if not record:
            raise ValueError("The active plan has no previous block to clean")
        continued_from = next(
            (
                item.config.get("metadata", {}).get("continued_from_plan_id")
                for item in self.state.block_lineage(record.id)
                if item.config.get("metadata", {}).get("continued_from_plan_id")
            ),
            None,
        )
        previous_id = continued_from or record.supersedes_plan_id
        previous = self.state.plan(previous_id) if previous_id else None
        if not previous or not self.plan_is_finished(previous):
            raise ValueError("The previous plan is not a finished training block")
        return record, previous

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
        with self.training_client() as connection:
            if self.training_provider_key() == "starter":
                preview = self._official_publisher(plan, connection, f"one-off:{draft_id}").preview_conflicts()
            else:
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
        with self.training_client() as connection:
            publisher = (
                self._official_publisher(plan, connection, f"one-off:{draft_id}")
                if self.training_provider_key() == "starter"
                else None
            )
            self._verify_conflict_preview(plan, connection, preview, plan_id=f"one-off:{draft_id}")
            apply_actions = (
                publisher.apply(schedule=True) if publisher else PlanApplier(plan, connection).apply(schedule=True)
            )
            conflict_actions = []
            if remove_conflicts:
                conflict_actions = (
                    publisher.apply_conflicts(preview, delete_templates=delete_templates)
                    if publisher
                    else ScheduledConflictCleanup(plan, connection, today=self.today).apply(
                        preview,
                        delete_templates=delete_templates,
                    )
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

    def plan_is_finished(self, record):
        if record is None:
            return False
        return bool(record.end_date < self.today or self.state.block_progress_summary(record.id)["remaining"] == 0)

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

    def _verify_conflict_preview(self, plan, connection, expected, plan_id="verification"):
        if self.training_provider_key() == "starter":
            current = self._official_publisher(plan, connection, plan_id).preview_conflicts()
        else:
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

    def _selectable_schedules_path(self, plan_id):
        return self._web_state_dir / f"cleanup-{int(plan_id)}-selectable-schedules.json"

    def _workout_library_path(self):
        return self._web_state_dir / "cleanup-workout-library.json"

    def _obsolete_cleanup_path(self, plan_id):
        return self._web_state_dir / f"cleanup-{int(plan_id)}-obsolete-templates.json"

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
