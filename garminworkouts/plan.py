import json
import time
from collections import defaultdict

from garminworkouts.models.running_workout import RunningWorkout
from garminworkouts.utils.functional import filter_empty


class PlanApplier:
    def __init__(self, plan, connection):
        self.plan = plan
        self.connection = connection

    def apply(self, schedule=True):
        planned_workouts = self.plan.unique_workouts()
        planned_names = {workout.get_workout_name() for workout in planned_workouts}
        existing_by_name = self._existing_workouts_by_name(planned_names)
        workout_ids = {}
        actions = []

        for workout in planned_workouts:
            name = workout.get_workout_name()
            existing = existing_by_name.get(name)
            if existing:
                workout_id = RunningWorkout.extract_workout_id(existing)
                owner_id = RunningWorkout.extract_workout_owner_id(existing)
                payload = workout.create_workout(workout_id, owner_id)
                self.connection.update_workout(workout_id, payload)
                actions.append({"action": "updated", "name": name, "workout_id": workout_id})
            else:
                payload = workout.create_workout()
                saved = self.connection.save_workout(payload)
                workout_id = self._workout_id_from_response(saved)
                if workout_id is None:
                    workout_id = self._find_created_workout_id(name)
                actions.append({"action": "created", "name": name, "workout_id": workout_id})
            workout_ids[name] = workout_id

        if schedule:
            scheduled = self._scheduled_workout_keys()
            for entry in self.plan.entries:
                workout = entry["workout"]
                name = workout.get_workout_name()
                workout_id = workout_ids[name]
                date_text = entry["date"].isoformat()
                schedule_key = (str(workout_id), date_text)
                if schedule_key in scheduled:
                    actions.append(
                        {"action": "schedule-skipped", "name": name, "workout_id": workout_id, "date": date_text}
                    )
                    continue
                self.connection.schedule_workout(workout_id, date_text)
                scheduled.add(schedule_key)
                actions.append({"action": "scheduled", "name": name, "workout_id": workout_id, "date": date_text})

        return actions

    def _existing_workouts_by_name(self, planned_names):
        workouts = defaultdict(list)
        for workout in self.connection.list_workouts():
            if not RunningWorkout.is_running(workout):
                continue
            name = RunningWorkout.extract_workout_name(workout)
            if name in planned_names:
                workouts[name].append(workout)
        duplicates = [name for name, items in workouts.items() if len(items) > 1]
        if duplicates:
            raise ValueError(f"Garmin Connect contains duplicate workout names: {', '.join(sorted(duplicates))}")
        return {name: items[0] for name, items in workouts.items()}

    def _find_created_workout_id(self, workout_name, attempts=5, delay_seconds=1):
        for attempt in range(attempts):
            matches = [
                RunningWorkout.extract_workout_id(workout)
                for workout in self.connection.list_workouts()
                if RunningWorkout.is_running(workout) and RunningWorkout.extract_workout_name(workout) == workout_name
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                break
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
        raise RuntimeError(
            f"Workout '{workout_name}' was created but its Garmin ID could not be resolved unambiguously"
        )

    @staticmethod
    def _workout_id_from_response(response):
        if isinstance(response, dict):
            if response.get("workoutId") is not None:
                return response["workoutId"]
            for value in response.values():
                workout_id = PlanApplier._workout_id_from_response(value)
                if workout_id is not None:
                    return workout_id
        if isinstance(response, list):
            for value in response:
                workout_id = PlanApplier._workout_id_from_response(value)
                if workout_id is not None:
                    return workout_id
        return None

    def _scheduled_workout_keys(self):
        months = {(entry["date"].year, entry["date"].month) for entry in self.plan.entries}
        keys = set()
        for year, month in months:
            response = self.connection.list_scheduled_workouts(year, month)
            keys.update(self._extract_scheduled_keys(response))
        return keys

    @classmethod
    def _extract_scheduled_keys(cls, value, inherited_date=None):
        keys = set()
        if isinstance(value, list):
            for item in value:
                keys.update(cls._extract_scheduled_keys(item, inherited_date))
            return keys
        if not isinstance(value, dict):
            return keys

        date_value = next(
            (value.get(key) for key in ("date", "calendarDate", "scheduledDate") if value.get(key)), inherited_date
        )
        workout_id = value.get("workoutId")
        if workout_id is None and isinstance(value.get("workout"), dict):
            workout_id = value["workout"].get("workoutId")
        if workout_id is not None and date_value:
            keys.add((str(workout_id), str(date_value)[:10]))

        for nested in value.values():
            if isinstance(nested, dict | list):
                keys.update(cls._extract_scheduled_keys(nested, date_value))
        return keys


def preview_plan(plan):
    preview = {
        "name": plan.name,
        "mode": "preview-only; no Garmin Connect changes were made",
        "workouts": [
            {
                "date": entry["date"].isoformat(),
                "payload": filter_empty(entry["workout"].create_workout()),
            }
            for entry in plan.entries
        ],
    }
    return json.dumps(preview, indent=2)
