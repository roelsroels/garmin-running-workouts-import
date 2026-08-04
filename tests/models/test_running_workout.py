import pytest

from garminworkouts.models.running_workout import RunningWorkout


def test_create_running_workout_with_pace_repeat_and_distance():
    workout = RunningWorkout(
        {
            "sport": "running",
            "name": "W1Q 6x2 525",
            "description": "Six controlled intervals",
            "steps": [
                {"type": "warmup", "duration": "15:00"},
                {
                    "repeat": 6,
                    "steps": [
                        {"type": "interval", "duration": "2:00", "pace": ["5:25", "5:30"]},
                        {"type": "recovery", "duration": "1:30"},
                    ],
                },
                {"type": "cooldown", "distance": "1km", "pace": "6:20-7:00"},
            ],
        }
    )

    payload = workout.create_workout(123, 456)
    assert payload["workoutId"] == 123
    assert payload["ownerId"] == 456
    assert payload["sportType"] == {"sportTypeId": 1, "sportTypeKey": "running"}
    assert payload["description"] == "Six controlled intervals"
    assert payload["estimatedDurationInSecs"] == 2559

    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[1]["type"] == "RepeatGroupDTO"
    assert steps[1]["numberOfIterations"] == 6
    interval = steps[1]["workoutSteps"][0]
    assert interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert interval["targetValueOne"] == pytest.approx(1000 / 325)
    assert interval["targetValueTwo"] == pytest.approx(1000 / 330)
    assert steps[2]["endCondition"] == {"conditionTypeId": 3, "conditionTypeKey": "distance"}
    assert steps[2]["endConditionValue"] == 1000


def test_create_running_workout_with_heart_rate_caps_range_and_zone():
    workout = RunningWorkout(
        {
            "name": "HR progression",
            "steps": [
                {"type": "warmup", "duration": "10:00", "heart_rate_max": 120},
                {"type": "interval", "duration": "15:00", "heart_rate": [120, 140]},
                {"type": "cooldown", "duration": "10:00", "heart_rate_zone": 2},
            ],
        }
    )

    steps = workout.create_workout()["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert (steps[0]["targetValueOne"], steps[0]["targetValueTwo"]) == (35, 120)
    assert (steps[1]["targetValueOne"], steps[1]["targetValueTwo"]) == (120, 140)
    assert steps[2]["zoneNumber"] == 2
    assert steps[2]["targetValueOne"] is None
    assert steps[2]["targetValueTwo"] is None


@pytest.mark.parametrize(
    "step",
    [
        {"type": "sprint", "duration": "1:00"},
        {"type": "interval", "duration": "1:00", "distance": "200m"},
        {"repeat": 1, "steps": [{"duration": "1:00"}]},
        {"type": "interval", "duration": "1:00", "power": 200},
        {"type": "interval", "duration": "1:00", "pace": "5:00-5:10", "heart_rate_max": 140},
        {"type": "interval", "duration": "1:00", "heart_rate_zone": 6},
    ],
)
def test_invalid_running_step(step):
    with pytest.raises(ValueError):
        RunningWorkout({"name": "Invalid", "steps": [step]})
