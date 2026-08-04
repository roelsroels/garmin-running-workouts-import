from copy import deepcopy
from datetime import date

from garminworkouts.models.running_workout import RunningWorkout


class TrainingPlan:
    def __init__(self, config):
        self.config = config
        self._entries = self._validate_and_build_entries()

    @property
    def name(self):
        return self.config.get("name", "Training plan")

    @property
    def entries(self):
        return list(self._entries)

    def unique_workouts(self):
        by_name = {}
        for entry in self._entries:
            workout = entry["workout"]
            name = workout.get_workout_name()
            payload = workout.create_workout()
            previous = by_name.get(name)
            if previous and previous.create_workout() != payload:
                raise ValueError(f"Workout name '{name}' is reused for different definitions")
            by_name[name] = workout
        return list(by_name.values())

    def _validate_and_build_entries(self):
        if not isinstance(self.config, dict):
            raise ValueError("Training plan must be a mapping")
        workouts = self.config.get("workouts")
        if not isinstance(workouts, list) or not workouts:
            raise ValueError("Training plan requires a non-empty workouts list")

        entries = []
        calendar_keys = set()
        for item in workouts:
            if not isinstance(item, dict):
                raise ValueError("Every planned workout must be a mapping")
            try:
                workout_date = date.fromisoformat(str(item["date"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Every planned workout requires a valid ISO date (YYYY-MM-DD)") from exc

            workout_config = deepcopy(item)
            workout_config.pop("date")
            sport = workout_config.get("sport", "running")
            if sport != "running":
                raise ValueError(f"Unsupported sport '{sport}'; this tool supports running workouts only")
            workout = RunningWorkout(workout_config)

            calendar_key = (workout_date.isoformat(), workout.get_workout_name())
            if calendar_key in calendar_keys:
                raise ValueError(f"Workout '{calendar_key[1]}' is listed twice on {calendar_key[0]}")
            calendar_keys.add(calendar_key)
            entries.append({"date": workout_date, "workout": workout})

        self._warn_if_watch_names_collide(entries)
        return entries

    @staticmethod
    def _warn_if_watch_names_collide(entries):
        visible_names = {}
        for entry in entries:
            name = entry["workout"].get_workout_name()
            visible = name[:15].casefold()
            previous = visible_names.get(visible)
            if previous and previous != name:
                raise ValueError(
                    f"Workout names '{previous}' and '{name}' share their first 15 characters; "
                    "make the watch-visible prefixes unique"
                )
            visible_names[visible] = name
