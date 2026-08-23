import getpass
import os
from datetime import date, timedelta
from pathlib import Path

from garminworkouts.activities import ActivityArchive, ActivitySummary, AssessmentSelector
from garminworkouts.fit_analysis import FitAnalyzer
from garminworkouts.garmin.garminclient import GarminClient
from garminworkouts.garmin.ratelimit import (
    GarminRateLimiter,
    GarminRateLimitError,
    has_reusable_tokens,
)
from garminworkouts.llm import LLMConfig, OpenAICompatibleAdvisor
from garminworkouts.models.heart_rate import HeartRateRange, validate_heart_rate_zone
from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.plan import PlanApplier
from garminworkouts.planner import DeterministicPlanner, write_plan
from garminworkouts.retire import PlanRetirement, ScheduledConflictCleanup
from garminworkouts.state import Goal

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
HEART_RATE_PHASES = (
    ("warmup", "Warm-up and cooldown"),
    ("easy", "Easy running"),
    ("long", "Long runs"),
    ("quality", "Quality work intervals"),
    ("recovery", "Recoveries between intervals"),
)


class Console:
    def write(self, message=""):
        print(message)

    def ask(self, prompt, default=None):
        suffix = f" [{default}]" if default not in (None, "") else ""
        answer = input(f"{prompt}{suffix}: ").strip()
        return answer or default

    def secret(self, prompt):
        return getpass.getpass(f"{prompt}: ")

    def confirm(self, prompt, default=False):
        suffix = "Y/n" if default else "y/N"
        answer = input(f"{prompt} [{suffix}]: ").strip().casefold()
        if not answer:
            return default
        return answer in {"y", "yes"}

    def choose(self, prompt, options, default=1):
        self.write(prompt)
        for index, option in enumerate(options, start=1):
            self.write(f"  {index}. {option}")
        while True:
            answer = self.ask("Choice", str(default))
            try:
                selected = int(answer)
            except (TypeError, ValueError):
                selected = 0
            if 1 <= selected <= len(options):
                return selected
            self.write(f"Enter a number between 1 and {len(options)}.")


class InteractiveApp:
    def __init__(self, state, console=None, today=None):
        self.state = state
        self.console = console or Console()
        self.today = today or date.today()
        self.planner = DeterministicPlanner()

    def run(self):
        self.console.write("Garmin Running Planner")
        self.console.write("======================")
        if not self.state.active_goal():
            self.console.write("No runner profile or active goal exists yet. Let's set it up.")
            self.setup()

        self._refresh_if_possible()
        while True:
            self.show_dashboard()
            active_plan = self.state.active_plan()
            options = [
                "Refresh completed activities",
                "Assess progress and adapt the remaining plan" if active_plan else "Create the first training plan",
                "Change the goal or availability",
                "View the complete planned calendar",
                "Review and clean Garmin schedule overlaps",
                "Create a one-off heart-rate workout",
                "Garmin connection and optional LLM settings",
                "Exit",
            ]
            choice = self.console.choose("What would you like to do?", options)
            try:
                if choice == 1:
                    self.refresh()
                elif choice == 2:
                    self.adapt() if active_plan else self.generate_plan()
                elif choice == 3:
                    self.change_goal()
                elif choice == 4:
                    self.show_calendar()
                elif choice == 5:
                    self.clean_schedule_overlaps()
                elif choice == 6:
                    self.create_heart_rate_workout()
                elif choice == 7:
                    self.settings()
                else:
                    return
            except (RuntimeError, ValueError) as exc:
                self.console.write(f"Unable to complete that action: {exc}")

    def setup(self):
        self.configure_garmin(test_connection=False)
        goal = self.goal_wizard()
        self.state.save_goal(goal)
        self.configure_llm()
        if self.console.confirm("Fetch Garmin history and create the first plan now?", default=True):
            self.generate_plan()

    def show_dashboard(self):
        goal = self.state.active_goal()
        plan = self.state.active_plan()
        self.console.write()
        self.console.write("Active goal")
        self.console.write(f"  {self._goal_summary(goal)}")
        if goal:
            days = ", ".join(WEEKDAYS[day][:3] for day in goal.available_days)
            self.console.write(
                f"  {goal.runs_per_week} runs/week · {days} · long run {WEEKDAYS[goal.long_run_day]} · "
                f"{goal.plan_weeks}-week block"
            )
            if goal.constraints:
                self.console.write(f"  Constraints for review: {goal.constraints}")
            if goal.heart_rate_targets:
                self.console.write(f"  Heart-rate guidance: {_format_heart_rate_targets(goal.heart_rate_targets)}")
        if not plan:
            self.console.write("Current block")
            self.console.write("  No plan has been applied yet.")
            return

        summary = self.state.block_progress_summary(plan.id)
        self.console.write("Current block")
        self.console.write(f"  {plan.start_date:%d %b %Y} – {plan.end_date:%d %b %Y}")
        self.console.write(
            f"  {summary['completed']} completed · {summary['missed']} missed · {summary['remaining']} remaining"
        )
        next_item = next(
            (
                item
                for item in self.state.progress(plan.id)
                if item["status"] == "scheduled" and date.fromisoformat(item["workout_date"]) >= self.today
            ),
            None,
        )
        if next_item:
            self.console.write("Next workout")
            self.console.write(f"  {next_item['workout_date']} · {next_item['workout_name']}")
        self.console.write(f"Planning confidence: {plan.confidence}")

    def show_calendar(self):
        plan = self.state.active_plan()
        if not plan:
            self.console.write("No active plan.")
            return
        progress = {(row["workout_date"], row["workout_name"]): row for row in self.state.progress(plan.id)}
        self.console.write(f"{plan.name}")
        for row in self.state.block_progress(plan.id):
            if date.fromisoformat(row["workout_date"]) < plan.start_date:
                score = self._execution_score_label(row)
                self.console.write(
                    f"  {row['workout_date']}  {row['status']:9}  {row['workout_name']}  prior block history{score}"
                )
        for workout in plan.config.get("workouts", []):
            row = progress.get((str(workout["date"]), workout["name"]), {})
            status = row.get("status", "unknown")
            score = self._execution_score_label(row)
            self.console.write(
                f"  {workout['date']}  {status:9}  {workout['name']}  {workout.get('description', '')}{score}"
            )

    def refresh(self, silent=False, connection=None):
        plan = self.state.active_plan()
        if not plan:
            if not silent:
                self.console.write("No active plan to refresh.")
            return []
        if connection is None:
            with self._garmin_client() as managed_connection:
                return self.refresh(silent=silent, connection=managed_connection)
        activities = self._fetch_activities(
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
        summary = self.state.refresh_progress(plan.id, activities, today=self.today)
        self.state.record_event(
            "progress-refreshed",
            {
                "plan_id": plan.id,
                "activity_count": len(activities),
                "execution_score_errors": score_errors,
            },
        )
        if not silent:
            counts = self.state.progress_summary(plan.id)
            self.console.write(
                f"Progress refreshed: {counts['completed']} completed, {counts['missed']} missed, "
                f"{counts['remaining']} remaining."
            )
        return summary

    @staticmethod
    def _execution_score_label(row):
        score = row.get("execution_score")
        if score is not None:
            return f" · execution {score:g}%"
        if row.get("status") == "completed":
            return (
                " · execution unavailable" if row.get("execution_score_checked_at") else " · execution pending refresh"
            )
        return ""

    def generate_plan(self, replace_active=False):
        goal = self.state.active_goal()
        if not goal:
            raise ValueError("Create a goal before generating a plan")
        activities = self._fetch_recent_history()
        proposal = self.planner.generate(goal, activities)
        plan_path = self._plan_path(proposal.config)
        write_plan(plan_path, proposal.config)
        old_plan = self.state.active_plan() if replace_active else None
        record = self.state.save_plan(
            goal,
            proposal.config,
            plan_path,
            proposal.confidence,
            proposal.rationale,
            supersedes_plan_id=old_plan.id if old_plan else None,
        )
        self._print_proposal(proposal)
        if not self.console.confirm("Apply and schedule this plan in Garmin Connect?", default=False):
            self.console.write(f"Proposal saved locally at {plan_path}")
            return record
        self._apply_replacement(record, old_plan)
        return record

    def adapt(self):
        goal = self.state.active_goal()
        active_plan = self.state.active_plan()
        if not goal or not active_plan:
            raise ValueError("An active goal and plan are required")
        with self._garmin_client() as connection:
            self.refresh(silent=True, connection=connection)
            progress = self.state.block_progress_summary(active_plan.id)
            if progress["completed"] < 2:
                self.console.write(
                    "Fewer than two scheduled runs are complete; retaining the current plan is recommended."
                )
                return None

            activities = self._fetch_recent_history(connection=connection)
            fit_analysis = self._prepare_fit_analysis(activities, connection=connection)
        proposal = self.planner.adapt_remaining(
            goal,
            active_plan,
            activities,
            today=self.today,
            fit_analysis=fit_analysis,
        )
        changes = self._calendar_changes(active_plan.config, proposal.config, today=self.today)
        if not changes:
            self.console.write("The new evidence does not justify changing any remaining workouts.")
            self.state.record_event("adaptation-retained", {"plan_id": active_plan.id})
            return None

        self._print_proposal(proposal)
        self.console.write("Proposed calendar changes")
        for change in changes:
            self.console.write(f"  {change}")
        self._optional_llm_explanation(proposal, progress, changes)
        if not self.console.confirm("Replace the remaining Garmin schedule with this proposal?", default=False):
            self.console.write("The current Garmin schedule was left unchanged.")
            return None

        plan_path = self._plan_path(proposal.config)
        write_plan(plan_path, proposal.config)
        record = self.state.save_plan(
            goal,
            proposal.config,
            plan_path,
            proposal.confidence,
            proposal.rationale,
            supersedes_plan_id=active_plan.id,
        )
        self._apply_replacement(record, active_plan)
        return record

    def change_goal(self):
        current = self.state.active_goal()
        goal = self.goal_wizard(current)
        self.state.save_goal(goal)
        self.console.write("The new goal is active. The existing Garmin schedule has not been changed yet.")
        if self.console.confirm("Generate and review a replacement plan now?", default=True):
            self.generate_plan(replace_active=True)

    def settings(self):
        choice = self.console.choose(
            "Settings",
            ["Update or reconnect Garmin", "Configure optional LLM", "Return"],
        )
        if choice == 1:
            self.configure_garmin(test_connection=True)
        elif choice == 2:
            self.configure_llm()

    def clean_schedule_overlaps(self):
        active_record = self.state.active_plan()
        if not active_record:
            self.console.write("There is no active local plan to protect during cleanup.")
            return []

        active_plan = TrainingPlan(active_record.config)
        with self._garmin_client() as connection:
            decision = self._review_schedule_conflicts(
                active_plan,
                connection,
                replacement_pending=False,
            )
            if decision["conflict_count"] == 0:
                self.console.write("No overlapping Garmin schedule entries were found.")
                return []
            if not decision["proceed"]:
                self.console.write("The Garmin schedule was left unchanged.")
                return []
            actions = decision["cleanup"].apply(
                decision["preview"],
                delete_templates=decision["delete_templates"],
            )

        self.state.record_event(
            "schedule-overlaps-cleaned",
            {
                "active_plan_id": active_record.id,
                "actions": actions,
            },
        )
        self.console.write("The overlapping Garmin schedule entries were removed.")
        return actions

    def create_heart_rate_workout(self):
        workout_date = _parse_date(
            self.console.ask(
                "Workout date (YYYY-MM-DD)",
                (self.today + timedelta(days=1)).isoformat(),
            )
        )
        if workout_date < self.today:
            raise ValueError("A new one-off workout cannot be scheduled in the past")
        name = self.console.ask("Workout name", f"{workout_date:%y%m%d} HR Run")
        description = self.console.ask("Workout description", "Interactive heart-rate workout")
        step_count = self._ask_int("Number of sequential steps", 2, 1, 20)
        step_types = ["warmup", "interval", "recovery", "cooldown"]
        target_types = ["Maximum BPM", "BPM range", "Garmin HR zone", "No HR target"]
        steps = []
        targeted_steps = 0
        for index in range(1, step_count + 1):
            self.console.write(f"Step {index}")
            step_type = step_types[
                self.console.choose("Step type", [item.title() for item in step_types], min(index, 4)) - 1
            ]
            duration = _clock(_parse_clock(self.console.ask("Duration (MM:SS or H:MM:SS)", "10:00")))
            target_choice = self.console.choose("Heart-rate target", target_types, 1)
            step = {"type": step_type, "duration": duration}
            if target_choice == 1:
                value = self.console.ask("Maximum BPM")
                step.update(_parse_heart_rate_target("heart_rate_max", value))
                targeted_steps += 1
            elif target_choice == 2:
                value = self.console.ask("BPM range, e.g. 120-140")
                step.update(_parse_heart_rate_target("heart_rate", value))
                targeted_steps += 1
            elif target_choice == 3:
                value = self.console.ask("Garmin zone 1-5")
                step.update(_parse_heart_rate_target("heart_rate_zone", value))
                targeted_steps += 1
            steps.append(step)
        if not targeted_steps:
            raise ValueError("A heart-rate workout must contain at least one HR-targeted step")

        config = {
            "name": f"One-off HR workout {workout_date.isoformat()}",
            "workouts": [
                {
                    "date": workout_date.isoformat(),
                    "sport": "running",
                    "name": name,
                    "description": description,
                    "steps": steps,
                }
            ],
        }
        plan = TrainingPlan(config)
        self.console.write("Proposed one-off workout")
        self.console.write(f"  {workout_date.isoformat()}  {name}  {description}")
        for index, step in enumerate(steps, 1):
            target = _format_step_heart_rate(step) or "no HR target"
            self.console.write(f"  Step {index}: {step['type']} {step['duration']} · {target}")
        if not self.console.confirm("Apply and schedule this workout in Garmin Connect?", default=False):
            self.console.write("No Garmin changes were made.")
            return None

        with self._garmin_client() as connection:
            decision = self._review_schedule_conflicts(plan, connection)
            if not decision["proceed"]:
                self.console.write("No Garmin changes were made.")
                return None
            apply_actions = PlanApplier(plan, connection).apply(schedule=True)
            cleanup_actions = []
            if decision["cleanup"] is not None:
                cleanup_actions = decision["cleanup"].apply(
                    decision["preview"],
                    delete_templates=decision["delete_templates"],
                )
        self.state.record_event(
            "one-off-heart-rate-workout-applied",
            {
                "date": workout_date.isoformat(),
                "name": name,
                "apply_actions": apply_actions,
                "conflict_cleanup_actions": cleanup_actions,
            },
        )
        self.console.write("The one-off heart-rate workout was scheduled successfully.")
        return config

    def configure_garmin(self, test_connection=True):
        current = self.state.get_setting("garmin_username", os.getenv("GARMIN_USERNAME", ""))
        username = self.console.ask("Garmin Connect username", current)
        if not username:
            raise ValueError("A Garmin username is required")
        self.state.set_setting("garmin_username", username)
        self.state.set_setting("garmin_token_store", str(self.state.tokens_dir))
        if not test_connection:
            return
        rate_limiter = GarminRateLimiter(self.state.tokens_dir)
        rate_limiter.check_cooldown()
        password = self.console.secret("Garmin password (not stored)")
        with GarminClient(
            username,
            password,
            str(self.state.tokens_dir),
            rate_limiter=rate_limiter,
        ) as connection:
            connection.list_recent_activities(1)
        self.console.write("Garmin connection succeeded; reusable session tokens were stored privately.")

    def configure_llm(self):
        enabled = self.console.confirm("Enable an optional OpenAI-compatible LLM explanation service?", default=False)
        if not enabled:
            self.state.set_setting("llm_provider", "none")
            return
        base_url = self.console.ask("OpenAI-compatible base URL", "https://api.openai.com/v1")
        model = self.console.ask("Model name")
        api_key_env = self.console.ask("Environment variable for the API key", "RUNNING_PLANNER_LLM_API_KEY")
        if not model:
            raise ValueError("A model name is required when the optional LLM is enabled")
        self.state.set_setting("llm_provider", "openai-compatible")
        self.state.set_setting("llm_base_url", base_url)
        self.state.set_setting("llm_model", model)
        self.state.set_setting("llm_api_key_env", api_key_env)
        self.console.write(
            f"LLM settings saved. The API key itself is not stored; set {api_key_env} or enter it when requested."
        )

    def goal_wizard(self, current=None):
        types = [
            ("complete_distance", "Complete a distance continuously"),
            ("target_time", "Run a distance within a target time"),
            ("sustain_pace", "Sustain a target pace"),
            ("endurance", "Improve general endurance"),
            ("speed", "Improve speed"),
            ("consistency", "Build running consistency"),
        ]
        default_type = next(
            (index for index, item in enumerate(types, 1) if current and item[0] == current.goal_type), 1
        )
        selection = self.console.choose("Primary goal", [label for _, label in types], default_type)
        goal_type, default_description = types[selection - 1]
        description = self.console.ask(
            "Short goal description", current.description if current else default_description
        )

        distance = current.target_distance_km if current else None
        target_time = current.target_time_seconds if current else None
        target_pace = current.target_pace_seconds_per_km if current else None
        target_duration = current.target_duration_minutes if current else None
        if goal_type in {"complete_distance", "target_time"}:
            distance = self._ask_float("Target distance in km", distance or 10.0)
        if goal_type == "target_time":
            target_time = _parse_clock(self.console.ask("Target time (MM:SS or H:MM:SS)", _clock(target_time or 3600)))
        if goal_type == "sustain_pace":
            target_pace = _parse_pace(self.console.ask("Target pace per km (M:SS)", _pace(target_pace or 360)))
            target_duration = self._ask_int(
                "Minutes to sustain that pace",
                target_duration or 20,
                5,
                180,
            )
        elif goal_type == "speed" and self.console.confirm("Do you have a specific target pace?", default=False):
            target_pace = _parse_pace(self.console.ask("Target pace per km (M:SS)"))

        start_default = current.start_date if current else self.today + timedelta(days=1)
        start_date = _parse_date(self.console.ask("Plan start date (YYYY-MM-DD)", start_default.isoformat()))
        weeks = self._ask_int("Planning period in weeks", current.plan_weeks if current else 4, 1, 12)
        runs = self._ask_int("Runs per week", current.runs_per_week if current else 3, 1, 7)
        day_default = _format_days(current.available_days) if current else "Tue,Thu,Sun"
        available_days = _parse_days(self.console.ask("Available running days", day_default))
        if len(available_days) < runs:
            raise ValueError("Provide at least as many available days as runs per week")
        long_default = WEEKDAYS[current.long_run_day] if current else WEEKDAYS[available_days[-1]]
        long_run_day = _parse_day(self.console.ask("Preferred long-run day", long_default))
        if long_run_day not in available_days:
            raise ValueError("The long-run day must be included in the available running days")
        max_minutes = self._ask_int(
            "Maximum normal session duration in minutes", current.max_session_minutes if current else 90, 15, 360
        )
        baseline_default = current.baseline_long_run_km if current else None
        baseline_text = self.console.ask("Current comfortable long-run distance in km (optional)", baseline_default)
        baseline_long = float(baseline_text) if baseline_text not in (None, "") else None
        heart_rate_targets, quality_target_preference = self.heart_rate_wizard(
            current.heart_rate_targets if current else {},
            current.quality_target_preference if current else "pace",
            has_quality_pace=bool(target_pace or (target_time and distance)),
        )
        constraints = self.console.ask("Optional training constraints", current.constraints if current else "") or ""
        target_date_default = current.target_date.isoformat() if current and current.target_date else ""
        target_date_text = self.console.ask("Target event/date (optional, YYYY-MM-DD)", target_date_default)
        target_date = _parse_date(target_date_text) if target_date_text else None

        return Goal(
            goal_type=goal_type,
            description=description,
            start_date=start_date,
            plan_weeks=weeks,
            available_days=available_days,
            long_run_day=long_run_day,
            target_date=target_date,
            target_distance_km=distance,
            target_time_seconds=target_time,
            target_pace_seconds_per_km=target_pace,
            target_duration_minutes=target_duration,
            runs_per_week=runs,
            max_session_minutes=max_minutes,
            baseline_long_run_km=baseline_long,
            heart_rate_targets=heart_rate_targets,
            quality_target_preference=quality_target_preference,
            constraints=constraints,
        )

    def heart_rate_wizard(self, current_targets=None, current_preference="pace", has_quality_pace=False):
        current_targets = current_targets or {}
        enabled = self.console.confirm(
            "Add heart-rate goals or limits to generated workouts?",
            default=bool(current_targets),
        )
        if not enabled:
            return {}, "pace"

        styles = [
            ("heart_rate_max", "Upper BPM caps"),
            ("heart_rate", "Custom BPM ranges"),
            ("heart_rate_zone", "Garmin heart-rate zones"),
        ]
        existing_key = next(
            (key for target in current_targets.values() for key in target if key in dict(styles)),
            "heart_rate_max",
        )
        default_style = next(index for index, item in enumerate(styles, 1) if item[0] == existing_key)
        selection = self.console.choose("Heart-rate target style", [label for _, label in styles], default_style)
        target_key = styles[selection - 1][0]
        self.console.write("Leave a phase empty to use no HR alert there; enter '-' to clear an existing value.")

        targets = {}
        for phase, label in HEART_RATE_PHASES:
            existing = current_targets.get(phase, {})
            default = _heart_rate_input_value(existing) if target_key in existing else None
            answer = self.console.ask(f"{label} {_heart_rate_prompt_suffix(target_key)}", default)
            if answer in (None, "") or str(answer).strip().casefold() in {"-", "none"}:
                continue
            targets[phase] = _parse_heart_rate_target(target_key, answer)
        if not targets:
            raise ValueError("Enable at least one heart-rate phase or answer no to heart-rate guidance")

        preference = "heart_rate" if "quality" in targets and not has_quality_pace else "pace"
        if "quality" in targets and has_quality_pace:
            default = 2 if current_preference == "heart_rate" else 1
            choice = self.console.choose(
                "Garmin permits one intensity target on a quality step. Which should be primary?",
                ["Goal pace", "Heart rate"],
                default,
            )
            preference = "pace" if choice == 1 else "heart_rate"
        return targets, preference

    def _fetch_recent_history(self, connection=None):
        return self._fetch_activities(
            self.today - timedelta(days=42),
            self.today,
            connection=connection,
        )

    def _prepare_fit_analysis(self, activities, connection=None):
        try:
            if connection is None:
                with self._garmin_client() as managed_connection:
                    analysis = prepare_fit_assessment(self.state, managed_connection, activities)
            else:
                analysis = prepare_fit_assessment(self.state, connection, activities)
        except GarminRateLimitError:
            raise
        except Exception as exc:
            self.console.write(f"FIT decoding was unavailable; continuing with Garmin summaries: {exc}")
            return {"activities": [], "failures": [{"error": str(exc)}]}
        self.state.record_event(
            "fit-assessment-created",
            {
                "decoded": len(analysis.get("activities", [])),
                "failures": len(analysis.get("failures", [])),
            },
        )
        return analysis

    def _fetch_activities(self, start_date, end_date, connection=None):
        if connection is None:
            with self._garmin_client() as managed_connection:
                raw = managed_connection.list_activities_by_date(
                    start_date.isoformat(), end_date.isoformat(), "running"
                )
        else:
            raw = connection.list_activities_by_date(start_date.isoformat(), end_date.isoformat(), "running")
        return sorted((ActivitySummary.from_garmin(item) for item in raw), key=lambda item: item.started_at)

    def _garmin_client(self):
        username = self.state.get_setting("garmin_username", os.getenv("GARMIN_USERNAME"))
        if not username:
            raise ValueError("Configure a Garmin username first")
        token_store = self.state.get_setting("garmin_token_store", str(self.state.tokens_dir))
        password = os.getenv("GARMIN_PASSWORD")
        token_path = Path(token_store)
        rate_limiter = GarminRateLimiter(token_path)
        rate_limiter.check_cooldown()
        if not password and not has_reusable_tokens(token_path):
            password = self.console.secret("Garmin password (not stored)")
        return GarminClient(username, password, token_store, rate_limiter=rate_limiter)

    def _refresh_if_possible(self):
        if not self.state.active_plan() or not has_reusable_tokens(self.state.tokens_dir):
            return
        try:
            self.refresh(silent=True)
        except Exception:
            self.console.write("Garmin refresh was unavailable; showing the most recently stored progress.")

    def _apply_replacement(self, new_record, old_record=None):
        new_plan = TrainingPlan(new_record.config)
        changes_started = False
        try:
            with self._garmin_client() as connection:
                conflict_decision = self._review_schedule_conflicts(new_plan, connection)
                if not conflict_decision["proceed"]:
                    self.console.write("No Garmin changes were made; the proposal remains saved locally.")
                    return False
                changes_started = True
                actions = PlanApplier(new_plan, connection).apply(schedule=True)
                conflict_actions = []
                if conflict_decision["cleanup"] is not None:
                    conflict_actions = conflict_decision["cleanup"].apply(
                        conflict_decision["preview"],
                        delete_templates=conflict_decision["delete_templates"],
                    )
                retirement_actions = []
                if old_record:
                    old_plan = TrainingPlan(old_record.config)
                    retirement = PlanRetirement(old_plan, connection, protected_plans=[new_plan], today=self.today)
                    retirement_preview = retirement.preview()
                    retirement_actions = retirement.apply(retirement_preview)
        except GarminRateLimitError:
            raise
        except Exception as exc:
            if not changes_started:
                raise RuntimeError(f"No Garmin changes were made. Details: {exc}") from exc
            self.state.mark_plan_failed(new_record.id, exc)
            raise RuntimeError(
                "The replacement was not fully applied. Existing activities were not deleted; "
                "review Garmin before retrying. "
                f"Details: {exc}"
            ) from exc

        self.state.activate_plan(new_record.id)
        self.state.record_event(
            "plan-applied",
            {
                "plan_id": new_record.id,
                "superseded_plan_id": old_record.id if old_record else None,
                "apply_actions": actions,
                "conflict_cleanup_actions": conflict_actions,
                "retirement_actions": retirement_actions,
            },
        )
        self.console.write("The plan was scheduled successfully in Garmin Connect.")
        return True

    def _review_schedule_conflicts(self, new_plan, connection, replacement_pending=True):
        cleanup = ScheduledConflictCleanup(new_plan, connection, today=self.today)
        preview = cleanup.preview()
        count = preview["summary"]["overlapping_calendar_entries"]
        if count == 0:
            return {
                "proceed": True,
                "cleanup": None,
                "preview": None,
                "delete_templates": False,
                "conflict_count": 0,
            }

        self.console.write("Existing Garmin schedule entries overlap this proposal")
        for item in preview["calendar"]:
            self.console.write(f"  {item['date']}  {item['name']}")
        timing = " after the new plan is uploaded" if replacement_pending else " now"
        remove = self.console.confirm(
            f"Remove these {count} existing calendar entr{'y' if count == 1 else 'ies'}{timing}?",
            default=True,
        )
        if not remove:
            if not replacement_pending:
                return {
                    "proceed": False,
                    "cleanup": None,
                    "preview": None,
                    "delete_templates": False,
                    "conflict_count": count,
                }
            proceed = self.console.confirm(
                "Continue and knowingly keep multiple scheduled workouts on those dates?",
                default=False,
            )
            return {
                "proceed": proceed,
                "cleanup": None,
                "preview": None,
                "delete_templates": False,
                "conflict_count": count,
            }

        unresolved = preview["summary"]["unresolved_calendar_entries"]
        if unresolved:
            raise RuntimeError(
                f"{unresolved} overlapping Garmin entry lacks a schedule ID; replacement is blocked before upload"
            )
        template_count = preview["summary"]["obsolete_template_candidates"]
        delete_templates = False
        if template_count:
            delete_templates = self.console.confirm(
                f"Also delete the {template_count} obsolete workout template"
                f"{'s' if template_count != 1 else ''} from the Garmin workout library? "
                "A template might be used on another date; completed activities are unaffected.",
                default=True,
            )
        return {
            "proceed": True,
            "cleanup": cleanup,
            "preview": preview,
            "delete_templates": delete_templates,
            "conflict_count": count,
        }

    def _optional_llm_explanation(self, proposal, progress, changes):
        config = LLMConfig(
            provider=self.state.get_setting("llm_provider", "none"),
            base_url=self.state.get_setting("llm_base_url", "https://api.openai.com/v1"),
            model=self.state.get_setting("llm_model", ""),
            api_key_env=self.state.get_setting("llm_api_key_env", "RUNNING_PLANNER_LLM_API_KEY"),
        )
        if not config.enabled or not self.console.confirm("Ask the optional LLM to explain this proposal?", False):
            return
        api_key = os.getenv(config.api_key_env) or self.console.secret("LLM API key (not stored)")
        assessment = {
            "goal": self.state.active_goal().to_dict(),
            "baseline": proposal.baseline.to_dict(),
            "progress": progress,
            "rationale": list(proposal.rationale),
            "calendar_changes": changes,
        }
        explanation = OpenAICompatibleAdvisor(config, api_key).explain(assessment)
        self.console.write("Optional LLM explanation")
        self.console.write(explanation)

    def _print_proposal(self, proposal):
        self.console.write("Proposed plan")
        self.console.write(f"  Confidence: {proposal.confidence}")
        for reason in proposal.rationale:
            self.console.write(f"  - {reason}")
        for workout in proposal.config["workouts"]:
            self.console.write(f"  {workout['date']}  {workout['name']}  {workout['description']}")

    def _plan_path(self, config):
        start = config["workouts"][0]["date"]
        slug = config["name"].lower().replace(" ", "-").replace("_", "-")
        slug = "".join(character for character in slug if character.isalnum() or character == "-")[:60]
        return self.state.plans_dir / f"{start}-{slug}.yaml"

    @staticmethod
    def _calendar_changes(old_config, new_config, today=None):
        today = today or date.today()
        old = {str(item["date"]): item for item in old_config.get("workouts", [])}
        new = {str(item["date"]): item for item in new_config.get("workouts", [])}
        changes = []
        for item in new.values():
            previous = old.get(str(item["date"]))
            if previous is None:
                changes.append(f"{item['date']}: add {item['name']}")
            elif previous.get("steps") != item.get("steps") or previous.get("description") != item.get("description"):
                changes.append(f"{item['date']}: {previous['name']} → {item['name']}")
        for item in old.values():
            item_date = date.fromisoformat(str(item["date"]))
            if item_date >= today and str(item["date"]) not in new:
                changes.append(f"{item['date']}: remove {item['name']}")
        return changes

    @staticmethod
    def _goal_summary(goal):
        if not goal:
            return "No active goal"
        details = []
        if goal.goal_type == "complete_distance" and goal.target_distance_km:
            details.append(f"{goal.target_distance_km:g} km")
        elif goal.goal_type == "target_time" and goal.target_distance_km and goal.target_time_seconds:
            details.append(f"{goal.target_distance_km:g} km in {_clock(goal.target_time_seconds)}")
        elif goal.goal_type == "sustain_pace":
            details.append(f"{goal.target_duration_minutes} min at {_pace(goal.target_pace_seconds_per_km)}/km")
        elif goal.goal_type == "speed" and goal.target_pace_seconds_per_km:
            details.append(f"target pace {_pace(goal.target_pace_seconds_per_km)}/km")
        target = goal.description
        if details:
            target += " · " + " · ".join(details)
        if goal.target_date:
            target += f" by {goal.target_date.isoformat()}"
        return target

    def _ask_int(self, prompt, default, minimum, maximum):
        value = int(self.console.ask(prompt, str(default)))
        if not minimum <= value <= maximum:
            raise ValueError(f"{prompt} must be between {minimum} and {maximum}")
        return value

    def _ask_float(self, prompt, default):
        value = float(self.console.ask(prompt, str(default)))
        if value <= 0:
            raise ValueError(f"{prompt} must be positive")
        return value


def prepare_fit_assessment(state, connection, activities):
    selection = AssessmentSelector().select(activities)
    if not selection.activities:
        raise ValueError("No activities are available for FIT assessment")
    destination = state.activities_dir / f"assessment-{selection.coverage_end.isoformat()}"
    ActivityArchive(destination).prepare(selection, connection)
    return FitAnalyzer().analyze_manifest(destination / "manifest.json")


def _parse_date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Dates must use YYYY-MM-DD") from exc


def _parse_clock(value):
    parts = [int(part) for part in str(value).split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError("Time must use MM:SS or H:MM:SS")
    if minutes < 0 or seconds < 0 or seconds >= 60 or (hours and minutes >= 60):
        raise ValueError("Invalid target time")
    return hours * 3600 + minutes * 60 + seconds


def _parse_pace(value):
    parts = str(value).split(":")
    if len(parts) != 2:
        raise ValueError("Pace must use M:SS per kilometre")
    minutes, seconds = (int(part) for part in parts)
    if minutes < 2 or seconds < 0 or seconds >= 60:
        raise ValueError("Invalid pace")
    return minutes * 60 + seconds


def _clock(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _pace(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}:{seconds:02d}"


def _parse_days(value):
    lookup = {name[:3].casefold(): index for index, name in enumerate(WEEKDAYS)}
    result = []
    for token in str(value).replace("/", ",").split(","):
        key = token.strip()[:3].casefold()
        if key not in lookup:
            raise ValueError(f"Unknown weekday '{token.strip()}'")
        result.append(lookup[key])
    return tuple(sorted(set(result)))


def _parse_day(value):
    days = _parse_days(value)
    if len(days) != 1:
        raise ValueError("Enter one long-run day")
    return days[0]


def _format_days(days):
    return ",".join(WEEKDAYS[day][:3] for day in days)


def _parse_heart_rate_target(target_key, value):
    if target_key == "heart_rate_max":
        heart_rate = HeartRateRange.from_maximum(value)
        return {target_key: heart_rate.upper}
    if target_key == "heart_rate":
        heart_rate = HeartRateRange.from_config(value)
        return {target_key: list(heart_rate.to_bpm_bounds())}
    return {target_key: validate_heart_rate_zone(int(value))}


def _heart_rate_prompt_suffix(target_key):
    return {
        "heart_rate_max": "maximum BPM (optional)",
        "heart_rate": "BPM range, e.g. 120-140 (optional)",
        "heart_rate_zone": "Garmin zone 1-5 (optional)",
    }[target_key]


def _heart_rate_input_value(target):
    if "heart_rate_max" in target:
        return str(target["heart_rate_max"])
    if "heart_rate" in target:
        return "-".join(str(value) for value in target["heart_rate"])
    if "heart_rate_zone" in target:
        return str(target["heart_rate_zone"])
    return None


def _format_heart_rate_targets(targets):
    labels = dict(HEART_RATE_PHASES)
    values = []
    for phase, target in targets.items():
        if "heart_rate_max" in target:
            description = f"≤{target['heart_rate_max']} bpm"
        elif "heart_rate" in target:
            description = f"{target['heart_rate'][0]}-{target['heart_rate'][1]} bpm"
        else:
            description = f"zone {target['heart_rate_zone']}"
        values.append(f"{labels.get(phase, phase)} {description}")
    return "; ".join(values)


def _format_step_heart_rate(step):
    if "heart_rate_max" in step:
        return f"HR ≤{step['heart_rate_max']} bpm"
    if "heart_rate" in step:
        return f"HR {step['heart_rate'][0]}-{step['heart_rate'][1]} bpm"
    if "heart_rate_zone" in step:
        return f"Garmin HR zone {step['heart_rate_zone']}"
    return ""
