from garminworkouts.models.distance import Distance
from garminworkouts.models.duration import Duration
from garminworkouts.models.heart_rate import HeartRateRange, validate_heart_rate_zone
from garminworkouts.models.pace import PaceRange


class RunningWorkout:
    _SPORT_TYPE = {"sportTypeId": 1, "sportTypeKey": "running"}

    _STEP_TYPES = {
        "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup"},
        "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
        "interval": {"stepTypeId": 3, "stepTypeKey": "interval"},
        "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery"},
        "rest": {"stepTypeId": 5, "stepTypeKey": "rest"},
        "other": {"stepTypeId": 7, "stepTypeKey": "other"},
    }
    _REPEAT_STEP_TYPE = {"stepTypeId": 6, "stepTypeKey": "repeat"}

    _NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
    _HEART_RATE_TARGET = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}
    _PACE_TARGET = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}

    def __init__(self, config):
        self.config = config
        self._validate_config()

    def get_workout_name(self):
        return self.config["name"]

    def create_workout(self, workout_id=None, workout_owner_id=None):
        payload = {
            "workoutId": workout_id,
            "ownerId": workout_owner_id,
            "workoutName": self.get_workout_name(),
            "description": self._description(),
            "sportType": self._SPORT_TYPE,
            "estimatedDurationInSecs": self._estimated_duration(self.config["steps"]),
            "workoutSegments": [
                {
                    "segmentOrder": 1,
                    "sportType": self._SPORT_TYPE,
                    "workoutSteps": self._steps(self.config["steps"]),
                }
            ],
        }
        return payload

    def _validate_config(self):
        if not isinstance(self.config, dict):
            raise ValueError("Workout definition must be a mapping")
        name = self.config.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Workout requires a non-empty name")
        if len(name) > 255:
            raise ValueError("Workout name cannot exceed 255 characters")
        steps = self.config.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"Running workout '{name}' requires at least one step")
        self._validate_steps(steps)

    def _validate_steps(self, steps):
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("Running steps must be mappings")
            if "repeat" in step:
                iterations = step["repeat"]
                if not isinstance(iterations, int) or isinstance(iterations, bool) or not 2 <= iterations <= 99:
                    raise ValueError("Repeat count must be an integer between 2 and 99")
                nested = step.get("steps")
                if not isinstance(nested, list) or not nested:
                    raise ValueError("A repeat group requires a non-empty steps list")
                self._validate_steps(nested)
                continue

            step_type = step.get("type", "interval")
            if step_type not in self._STEP_TYPES:
                allowed = ", ".join(self._STEP_TYPES)
                raise ValueError(f"Unknown running step type '{step_type}', expected one of: {allowed}")

            end_conditions = [key for key in ("duration", "distance", "lap_button") if step.get(key)]
            if len(end_conditions) > 1:
                raise ValueError("A running step can use only one of duration, distance, or lap_button")
            if "duration" in step:
                Duration(str(step["duration"])).to_seconds()
            if "distance" in step:
                Distance(step["distance"]).to_metres()
            if "pace" in step:
                PaceRange.from_config(step["pace"])

            target_fields = [
                key for key in ("pace", "heart_rate", "heart_rate_max", "heart_rate_zone") if step.get(key) is not None
            ]
            if len(target_fields) > 1:
                raise ValueError("A running step can have only one primary intensity target")
            if "heart_rate" in step:
                HeartRateRange.from_config(step["heart_rate"])
            if "heart_rate_max" in step:
                HeartRateRange.from_maximum(step["heart_rate_max"])
            if "heart_rate_zone" in step:
                validate_heart_rate_zone(step["heart_rate_zone"])

            supported = {
                "type",
                "duration",
                "distance",
                "lap_button",
                "pace",
                "heart_rate",
                "heart_rate_max",
                "heart_rate_zone",
                "description",
            }
            unknown = set(step) - supported
            if unknown:
                raise ValueError(f"Unsupported running step fields: {', '.join(sorted(unknown))}")

    def _description(self):
        return self.config.get("description") or "Running workout generated by garmin-running-workouts-import"

    def _steps(self, steps_config):
        steps, _, _ = self._steps_recursive(steps_config, 0, 0)
        return steps

    def _steps_recursive(self, steps_config, step_order, child_step_id):
        steps = []
        for step_config in steps_config:
            step_order += 1
            if "repeat" in step_config:
                child_step_id += 1
                repeat_order = step_order
                repeat_child_step_id = child_step_id
                nested_steps, step_order, child_step_id = self._steps_recursive(
                    step_config["steps"], step_order, child_step_id
                )
                steps.append(
                    {
                        "type": "RepeatGroupDTO",
                        "stepOrder": repeat_order,
                        "stepType": self._REPEAT_STEP_TYPE,
                        "childStepId": repeat_child_step_id,
                        "numberOfIterations": step_config["repeat"],
                        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
                        "endConditionValue": step_config["repeat"],
                        "workoutSteps": nested_steps,
                        "smartRepeat": False,
                    }
                )
            else:
                steps.append(self._executable_step(step_config, child_step_id or None, step_order))
        return steps, step_order, child_step_id

    def _executable_step(self, step_config, child_step_id, step_order):
        value_one, value_two = self._target_values(step_config)
        step = {
            "type": "ExecutableStepDTO",
            "stepOrder": step_order,
            "stepType": self._STEP_TYPES[step_config.get("type", "interval")],
            "childStepId": child_step_id,
            "endCondition": self._end_condition(step_config),
            "endConditionValue": self._end_condition_value(step_config),
            "targetType": self._target_type(step_config),
            "targetValueOne": value_one,
            "targetValueTwo": value_two,
            "zoneNumber": self._zone_number(step_config),
        }
        if step_config.get("description"):
            step["description"] = step_config["description"]
        return step

    @staticmethod
    def _end_condition(step_config):
        if step_config.get("duration") is not None:
            return {"conditionTypeId": 2, "conditionTypeKey": "time"}
        if step_config.get("distance") is not None:
            return {"conditionTypeId": 3, "conditionTypeKey": "distance"}
        return {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}

    @staticmethod
    def _end_condition_value(step_config):
        if step_config.get("duration") is not None:
            return Duration(str(step_config["duration"])).to_seconds()
        if step_config.get("distance") is not None:
            return Distance(step_config["distance"]).to_metres()
        return None

    def _target_type(self, step_config):
        if step_config.get("pace"):
            return self._PACE_TARGET
        if any(step_config.get(key) is not None for key in ("heart_rate", "heart_rate_max", "heart_rate_zone")):
            return self._HEART_RATE_TARGET
        return self._NO_TARGET

    @staticmethod
    def _target_values(step_config):
        if step_config.get("pace"):
            return PaceRange.from_config(step_config["pace"]).to_speed_bounds()
        if step_config.get("heart_rate") is not None:
            return HeartRateRange.from_config(step_config["heart_rate"]).to_bpm_bounds()
        if step_config.get("heart_rate_max") is not None:
            return HeartRateRange.from_maximum(step_config["heart_rate_max"]).to_bpm_bounds()
        return None, None

    @staticmethod
    def _zone_number(step_config):
        if step_config.get("heart_rate_zone") is None:
            return None
        return validate_heart_rate_zone(step_config["heart_rate_zone"])

    def _estimated_duration(self, steps_config):
        total = 0
        for step in steps_config:
            if "repeat" in step:
                total += step["repeat"] * self._estimated_duration(step["steps"])
            elif step.get("duration") is not None:
                total += Duration(str(step["duration"])).to_seconds()
            elif step.get("distance") is not None and step.get("pace"):
                pace = PaceRange.from_config(step["pace"])
                average_speed = sum(pace.to_speed_bounds()) / 2
                total += round(Distance(step["distance"]).to_metres() / average_speed)
        return total
