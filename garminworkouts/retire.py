from collections import defaultdict
from datetime import date

from garminworkouts.models.running_workout import RunningWorkout
from garminworkouts.models.training_plan import TrainingPlan


def combined_training_plan(records, name="Training block"):
    """Combine a block's plan revisions, preferring the newest definition of each workout name."""
    workouts = []
    seen_names = set()
    for record in records:
        for workout in record.config.get("workouts", []):
            workout_name = str(workout.get("name", ""))
            if not workout_name or workout_name in seen_names:
                continue
            seen_names.add(workout_name)
            workouts.append(workout)
    return TrainingPlan({"name": name, "workouts": workouts})


class ScheduledConflictCleanup:
    """Find and retire Garmin schedules that overlap a replacement plan."""

    def __init__(self, replacement_plan, connection, today=None):
        self.replacement_plan = replacement_plan
        self.connection = connection
        self.today = today or date.today()

    def preview(self):
        entries = [entry for entry in self.replacement_plan.entries if entry["date"] >= self.today]
        target_dates = {entry["date"].isoformat() for entry in entries}
        protected_keys = {(entry["date"].isoformat(), entry["workout"].get_workout_name()) for entry in entries}
        protected_names = {entry["workout"].get_workout_name() for entry in entries}
        workouts_by_id = {}
        for workout in self.connection.list_workouts():
            if RunningWorkout.is_running(workout):
                workouts_by_id[str(RunningWorkout.extract_workout_id(workout))] = workout

        calendar = []
        seen = set()
        protected_calendar_keys_seen = set()
        for item in self._scheduled_items(entries):
            if item["date"] not in target_dates:
                continue
            key = (str(item["scheduled_workout_id"]), str(item["workout_id"]), item["date"])
            if key in seen:
                continue
            seen.add(key)
            workout = workouts_by_id.get(str(item["workout_id"]))
            if workout is None:
                continue
            name = RunningWorkout.extract_workout_name(workout)
            calendar_key = (item["date"], name)
            if calendar_key in protected_keys and calendar_key not in protected_calendar_keys_seen:
                protected_calendar_keys_seen.add(calendar_key)
                continue
            calendar.append(
                {
                    "date": item["date"],
                    "name": name,
                    "workout_id": item["workout_id"],
                    "scheduled_workout_id": item["scheduled_workout_id"],
                    "action": "unschedule" if item["scheduled_workout_id"] is not None else "unresolved-schedule-id",
                    "template_can_be_deleted": name not in protected_names,
                }
            )

        templates = {}
        for item in calendar:
            if item["template_can_be_deleted"]:
                templates[str(item["workout_id"])] = {
                    "workout_id": item["workout_id"],
                    "name": item["name"],
                }
        calendar.sort(key=lambda item: (item["date"], item["name"]))
        return {
            "calendar": calendar,
            "templates": sorted(templates.values(), key=lambda item: item["name"]),
            "summary": {
                "overlapping_calendar_entries": len(calendar),
                "unresolved_calendar_entries": sum(item["action"] == "unresolved-schedule-id" for item in calendar),
                "obsolete_template_candidates": len(templates),
            },
            "warnings": [
                "Only scheduled workouts on replacement dates are included.",
                "Completed activity/FIT records are never deleted.",
            ],
        }

    def apply(self, preview, delete_templates=False):
        unresolved = [item for item in preview["calendar"] if item["action"] == "unresolved-schedule-id"]
        if unresolved:
            raise RuntimeError("Cannot replace schedules while a Garmin schedule ID is unresolved")
        actions = []
        for item in preview["calendar"]:
            self.connection.unschedule_workout(item["scheduled_workout_id"])
            actions.append(
                {
                    "action": "unscheduled-conflict",
                    "date": item["date"],
                    "name": item["name"],
                    "workout_id": item["workout_id"],
                    "scheduled_workout_id": item["scheduled_workout_id"],
                }
            )
        if delete_templates:
            for item in preview["templates"]:
                self.connection.delete_workout(item["workout_id"])
                actions.append(
                    {
                        "action": "deleted-conflicting-workout-template",
                        "name": item["name"],
                        "workout_id": item["workout_id"],
                    }
                )
        return actions

    def _scheduled_items(self, entries):
        months = {(entry["date"].year, entry["date"].month) for entry in entries}
        items = []
        for year, month in sorted(months):
            response = self.connection.list_scheduled_workouts(year, month)
            items.extend(PlanRetirement._extract_scheduled_items(response))
        return items


class PlanRetirement:
    def __init__(
        self,
        plan,
        connection,
        protected_plans=None,
        today=None,
        immutable_workouts=None,
        delete_finished_templates=False,
    ):
        self.plan = plan
        self.connection = connection
        self.protected_plans = protected_plans or []
        self.today = today or date.today()
        self.immutable_workouts = set(immutable_workouts or ())
        self.delete_finished_templates = delete_finished_templates

    def preview(self):
        plan_entries = self.plan.entries
        target_names = {entry["workout"].get_workout_name() for entry in plan_entries}
        immutable_names = {
            entry["workout"].get_workout_name()
            for entry in plan_entries
            if entry["date"] < self.today
            or (entry["date"].isoformat(), entry["workout"].get_workout_name()) in self.immutable_workouts
        }
        if self.delete_finished_templates:
            immutable_names.clear()
        protected_names, protected_calendar_keys = self._protected_keys()
        existing_by_name = self._existing_workouts_by_name(target_names)

        workouts = []
        workout_ids_by_name = {}
        for name in sorted(target_names):
            existing = existing_by_name.get(name)
            if existing is None:
                workouts.append({"name": name, "workout_id": None, "action": "missing"})
                continue
            workout_id = RunningWorkout.extract_workout_id(existing)
            workout_ids_by_name[name] = str(workout_id)
            if name in protected_names:
                action = "retain-protected"
            elif name in immutable_names:
                action = "retain-immutable"
            else:
                action = "delete"
            workouts.append({"name": name, "workout_id": workout_id, "action": action})

        scheduled_items = self._scheduled_items_for_plan_months()
        scheduled_by_key = defaultdict(list)
        for item in scheduled_items:
            scheduled_by_key[(str(item["workout_id"]), item["date"])].append(item)

        calendar = []
        for entry in sorted(plan_entries, key=lambda item: item["date"]):
            name = entry["workout"].get_workout_name()
            date_text = entry["date"].isoformat()
            workout_id = workout_ids_by_name.get(name)
            if workout_id is None:
                calendar.append(
                    {
                        "date": date_text,
                        "name": name,
                        "workout_id": None,
                        "scheduled_workout_id": None,
                        "action": "unresolved-missing-template",
                    }
                )
                continue

            matches = scheduled_by_key.get((workout_id, date_text), [])
            if not matches:
                calendar.append(
                    {
                        "date": date_text,
                        "name": name,
                        "workout_id": int(workout_id),
                        "scheduled_workout_id": None,
                        "action": "not-scheduled",
                    }
                )
                continue

            for match in matches:
                calendar_key = (date_text, name)
                if calendar_key in protected_calendar_keys:
                    action = "retain-protected"
                elif entry["date"] < self.today:
                    action = "retain-past"
                elif calendar_key in self.immutable_workouts:
                    action = "retain-immutable"
                elif match["scheduled_workout_id"] is None:
                    action = "unresolved-schedule-id"
                else:
                    action = "unschedule"
                calendar.append(
                    {
                        "date": date_text,
                        "name": name,
                        "workout_id": int(workout_id),
                        "scheduled_workout_id": match["scheduled_workout_id"],
                        "action": action,
                    }
                )

        unresolved = [item for item in calendar if item["action"] == "unresolved-schedule-id"]
        warnings = []
        if unresolved:
            warnings.append("One or more future schedules lack a Garmin schedule ID; apply is blocked.")
        if self.delete_finished_templates:
            warnings.append(
                "Obsolete workout-library templates from the finished plan will be deleted after replacement upload."
            )
        warnings.append("Completed activity/FIT records are outside this operation and will not be deleted.")

        return {
            "plan": self.plan.name,
            "today": self.today.isoformat(),
            "mode": "preview-only; no Garmin Connect changes were made",
            "summary": {
                "future_calendar_entries_to_unschedule": sum(item["action"] == "unschedule" for item in calendar),
                "past_calendar_entries_retained": sum(item["action"] == "retain-past" for item in calendar),
                "immutable_calendar_entries_retained": sum(item["action"] == "retain-immutable" for item in calendar),
                "workout_templates_to_delete": sum(item["action"] == "delete" for item in workouts),
                "protected_workout_templates": sum(item["action"] == "retain-protected" for item in workouts),
                "immutable_workout_templates": sum(item["action"] == "retain-immutable" for item in workouts),
                "missing_workout_templates": sum(item["action"] == "missing" for item in workouts),
            },
            "calendar": calendar,
            "workouts": workouts,
            "warnings": warnings,
        }

    def apply(self, preview=None):
        report = preview or self.preview()
        unresolved = [item for item in report["calendar"] if item["action"] == "unresolved-schedule-id"]
        if unresolved:
            raise RuntimeError("Cannot retire plan while a future Garmin schedule ID is unresolved")

        actions = []
        for item in report["calendar"]:
            if item["action"] != "unschedule":
                continue
            self.connection.unschedule_workout(item["scheduled_workout_id"])
            actions.append(
                {
                    "action": "unscheduled",
                    "date": item["date"],
                    "name": item["name"],
                    "workout_id": item["workout_id"],
                    "scheduled_workout_id": item["scheduled_workout_id"],
                }
            )

        actions.extend(self.apply_templates(report))
        return actions

    def apply_templates(self, preview=None):
        """Delete only workout-library templates selected by a retirement preview."""
        report = preview or self.preview()
        actions = []
        for item in report["workouts"]:
            if item["action"] != "delete":
                continue
            self.connection.delete_workout(item["workout_id"])
            actions.append(
                {
                    "action": "deleted-workout-template",
                    "name": item["name"],
                    "workout_id": item["workout_id"],
                }
            )
        return actions

    def _protected_keys(self):
        names = set()
        calendar_keys = set()
        for plan in self.protected_plans:
            for entry in plan.entries:
                name = entry["workout"].get_workout_name()
                names.add(name)
                calendar_keys.add((entry["date"].isoformat(), name))
        return names, calendar_keys

    def _existing_workouts_by_name(self, target_names):
        workouts = defaultdict(list)
        for workout in self.connection.list_workouts():
            if not RunningWorkout.is_running(workout):
                continue
            name = RunningWorkout.extract_workout_name(workout)
            if name in target_names:
                workouts[name].append(workout)
        duplicates = [name for name, items in workouts.items() if len(items) > 1]
        if duplicates:
            raise ValueError(f"Garmin Connect contains duplicate target workout names: {', '.join(sorted(duplicates))}")
        return {name: items[0] for name, items in workouts.items()}

    def _scheduled_items_for_plan_months(self):
        months = {(entry["date"].year, entry["date"].month) for entry in self.plan.entries}
        items = {}
        for year, month in sorted(months):
            response = self.connection.list_scheduled_workouts(year, month)
            for item in self._extract_scheduled_items(response):
                key = (item["scheduled_workout_id"], item["workout_id"], item["date"])
                items[key] = item
        return list(items.values())

    @classmethod
    def _extract_scheduled_items(cls, value, inherited_date=None):
        items = []
        if isinstance(value, list):
            for item in value:
                items.extend(cls._extract_scheduled_items(item, inherited_date))
            return items
        if not isinstance(value, dict):
            return items

        date_value = next(
            (value.get(key) for key in ("date", "calendarDate", "scheduledDate") if value.get(key)), inherited_date
        )
        workout_id = value.get("workoutId")
        if workout_id is None and isinstance(value.get("workout"), dict):
            workout_id = value["workout"].get("workoutId")
        if workout_id is not None and date_value:
            schedule_id = next(
                (
                    value.get(key)
                    for key in ("scheduledWorkoutId", "workoutScheduleId", "calendarId", "id")
                    if value.get(key) is not None
                ),
                None,
            )
            items.append(
                {
                    "scheduled_workout_id": schedule_id,
                    "workout_id": workout_id,
                    "date": str(date_value)[:10],
                }
            )

        for nested in value.values():
            if isinstance(nested, dict | list):
                items.extend(cls._extract_scheduled_items(nested, date_value))
        return items
