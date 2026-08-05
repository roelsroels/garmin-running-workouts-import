from collections import defaultdict
from datetime import date

from garminworkouts.models.running_workout import RunningWorkout


class PlanRetirement:
    def __init__(self, plan, connection, protected_plans=None, today=None):
        self.plan = plan
        self.connection = connection
        self.protected_plans = protected_plans or []
        self.today = today or date.today()

    def preview(self):
        plan_entries = self.plan.entries
        target_names = {entry["workout"].get_workout_name() for entry in plan_entries}
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
            action = "retain-protected" if name in protected_names else "delete"
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
        warnings.append("Completed activity/FIT records are outside this operation and will not be deleted.")

        return {
            "plan": self.plan.name,
            "today": self.today.isoformat(),
            "mode": "preview-only; no Garmin Connect changes were made",
            "summary": {
                "future_calendar_entries_to_unschedule": sum(item["action"] == "unschedule" for item in calendar),
                "past_calendar_entries_retained": sum(item["action"] == "retain-past" for item in calendar),
                "workout_templates_to_delete": sum(item["action"] == "delete" for item in workouts),
                "protected_workout_templates": sum(item["action"] == "retain-protected" for item in workouts),
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
