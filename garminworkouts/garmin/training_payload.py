"""Translate the provider-neutral running model to Garmin's official Training API."""

from garminworkouts.models.distance import Distance
from garminworkouts.models.duration import Duration
from garminworkouts.models.heart_rate import HeartRateRange, validate_heart_rate_zone
from garminworkouts.models.pace import PaceRange

WORKOUT_PROVIDER = "RunningPlanner"

_INTENSITIES = {
    "warmup": "WARMUP",
    "cooldown": "COOLDOWN",
    "interval": "INTERVAL",
    "recovery": "RECOVERY",
    "rest": "REST",
    "other": "ACTIVE",
}


def official_running_workout_payload(workout):
    """Build a single-sport running workout accepted by the official API."""
    counter = [0]
    steps = _steps(workout.config["steps"], counter)
    if counter[0] > 100:
        raise ValueError("Official Garmin running workouts cannot exceed 100 steps")
    return {
        "workoutName": workout.get_workout_name(),
        "description": workout._description()[:1024],
        "sport": "RUNNING",
        "poolLength": None,
        "poolLengthUnit": None,
        "workoutProvider": WORKOUT_PROVIDER,
        "workoutSourceId": WORKOUT_PROVIDER,
        "isSessionTransitionEnabled": False,
        "segments": [
            {
                "segmentOrder": 1,
                "sport": "RUNNING",
                "poolLength": None,
                "poolLengthUnit": None,
                "steps": steps,
            }
        ],
    }


def _steps(configured_steps, counter):
    result = []
    for configured in configured_steps:
        counter[0] += 1
        order = counter[0]
        if "repeat" in configured:
            result.append(
                {
                    "type": "WorkoutRepeatStep",
                    "stepOrder": order,
                    "repeatType": "REPEAT_UNTIL_STEPS_CMPLT",
                    "repeatValue": configured["repeat"],
                    "skipLastRestStep": False,
                    "steps": _steps(configured["steps"], counter),
                }
            )
            continue
        result.append(_workout_step(configured, order))
    return result


def _workout_step(configured, order):
    duration_type, duration_value, duration_value_type = _duration(configured)
    target_type, target_value, target_low, target_high = _target(configured)
    return {
        "type": "WorkoutStep",
        "stepOrder": order,
        "intensity": _INTENSITIES[configured.get("type", "interval")],
        "description": str(configured.get("description") or "")[:512],
        "durationType": duration_type,
        "durationValue": duration_value,
        "durationValueType": duration_value_type,
        "targetType": target_type,
        "targetValue": target_value,
        "targetValueLow": target_low,
        "targetValueHigh": target_high,
        "targetValueType": None,
        "secondaryTargetType": None,
        "secondaryTargetValue": None,
        "secondaryTargetValueLow": None,
        "secondaryTargetValueHigh": None,
        "secondaryTargetValueType": None,
    }


def _duration(configured):
    if configured.get("duration") is not None:
        return "TIME", Duration(str(configured["duration"])).to_seconds(), None
    if configured.get("distance") is not None:
        return "DISTANCE", Distance(configured["distance"]).to_metres(), "METER"
    return "OPEN", None, None


def _target(configured):
    if configured.get("pace") is not None:
        speeds = sorted(PaceRange.from_config(configured["pace"]).to_speed_bounds())
        return "PACE", None, speeds[0], speeds[1]
    if configured.get("heart_rate") is not None:
        heart_rate = HeartRateRange.from_config(configured["heart_rate"])
        return "HEART_RATE", None, heart_rate.lower, heart_rate.upper
    if configured.get("heart_rate_max") is not None:
        heart_rate = HeartRateRange.from_maximum(configured["heart_rate_max"])
        return "HEART_RATE", None, heart_rate.lower, heart_rate.upper
    if configured.get("heart_rate_zone") is not None:
        return "HEART_RATE", validate_heart_rate_zone(configured["heart_rate_zone"]), None, None
    return "OPEN", None, None, None
