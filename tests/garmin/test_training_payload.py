import pytest

from garminworkouts.garmin.training_payload import official_running_workout_payload
from garminworkouts.models.running_workout import RunningWorkout


def test_official_payload_maps_running_steps_repeats_hr_and_pace():
    workout = RunningWorkout(
        {
            "name": "Controlled intervals",
            "description": "A structured running workout",
            "steps": [
                {"type": "warmup", "duration": "10:00", "heart_rate_max": 120},
                {
                    "repeat": 3,
                    "steps": [
                        {"type": "interval", "duration": "03:00", "pace": "5:20-5:30"},
                        {"type": "recovery", "duration": "01:30", "heart_rate": "110-125"},
                    ],
                },
                {"type": "cooldown", "distance": "1 km", "heart_rate_zone": 2},
            ],
        }
    )

    payload = official_running_workout_payload(workout)

    assert payload["sport"] == "RUNNING"
    assert payload["workoutProvider"] == "RunningPlanner"
    steps = payload["segments"][0]["steps"]
    assert steps[0] == {
        "type": "WorkoutStep",
        "stepOrder": 1,
        "intensity": "WARMUP",
        "description": "",
        "durationType": "TIME",
        "durationValue": 600,
        "durationValueType": None,
        "targetType": "HEART_RATE",
        "targetValue": None,
        "targetValueLow": 35,
        "targetValueHigh": 120,
        "targetValueType": None,
        "secondaryTargetType": None,
        "secondaryTargetValue": None,
        "secondaryTargetValueLow": None,
        "secondaryTargetValueHigh": None,
        "secondaryTargetValueType": None,
    }
    repeat = steps[1]
    assert repeat["type"] == "WorkoutRepeatStep"
    assert repeat["stepOrder"] == 2
    assert repeat["repeatValue"] == 3
    assert [step["stepOrder"] for step in repeat["steps"]] == [3, 4]
    pace = repeat["steps"][0]
    assert pace["targetType"] == "PACE"
    assert pace["targetValueLow"] < pace["targetValueHigh"]
    recovery = repeat["steps"][1]
    assert (recovery["targetValueLow"], recovery["targetValueHigh"]) == (110, 125)
    cooldown = steps[2]
    assert cooldown["stepOrder"] == 5
    assert cooldown["durationType"] == "DISTANCE"
    assert cooldown["durationValue"] == 1000
    assert cooldown["durationValueType"] == "METER"
    assert cooldown["targetValue"] == 2


def test_official_payload_rejects_more_than_one_hundred_steps():
    workout = RunningWorkout(
        {
            "name": "Too many steps",
            "steps": [{"type": "interval", "duration": "00:10"} for _ in range(101)],
        }
    )

    with pytest.raises(ValueError, match="100 steps"):
        official_running_workout_payload(workout)
