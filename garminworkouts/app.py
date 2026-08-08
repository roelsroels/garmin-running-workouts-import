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
from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.plan import PlanApplier
from garminworkouts.planner import DeterministicPlanner, write_plan
from garminworkouts.retire import PlanRetirement
from garminworkouts.state import Goal

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


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
                self.console.write(
                    f"  {row['workout_date']}  {row['status']:9}  {row['workout_name']}  prior block history"
                )
        for workout in plan.config.get("workouts", []):
            row = progress.get((str(workout["date"]), workout["name"]), {})
            status = row.get("status", "unknown")
            self.console.write(f"  {workout['date']}  {status:9}  {workout['name']}  {workout.get('description', '')}")

    def refresh(self, silent=False, connection=None):
        plan = self.state.active_plan()
        if not plan:
            if not silent:
                self.console.write("No active plan to refresh.")
            return []
        activities = self._fetch_activities(
            plan.start_date - timedelta(days=1),
            self.today + timedelta(days=1),
            connection=connection,
        )
        summary = self.state.refresh_progress(plan.id, activities, today=self.today)
        self.state.record_event("progress-refreshed", {"plan_id": plan.id, "activity_count": len(activities)})
        if not silent:
            counts = self.state.progress_summary(plan.id)
            self.console.write(
                f"Progress refreshed: {counts['completed']} completed, {counts['missed']} missed, "
                f"{counts['remaining']} remaining."
            )
        return summary

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
            constraints=constraints,
        )

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
        try:
            with self._garmin_client() as connection:
                actions = PlanApplier(new_plan, connection).apply(schedule=True)
                retirement_actions = []
                if old_record:
                    old_plan = TrainingPlan(old_record.config)
                    retirement = PlanRetirement(old_plan, connection, protected_plans=[new_plan], today=self.today)
                    retirement_preview = retirement.preview()
                    retirement_actions = retirement.apply(retirement_preview)
        except Exception as exc:
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
                "retirement_actions": retirement_actions,
            },
        )
        self.console.write("The plan was scheduled successfully in Garmin Connect.")

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
