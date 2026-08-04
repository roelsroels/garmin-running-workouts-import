import pytest

from garminworkouts.models.training_plan import TrainingPlan


def _workout(date, name="W1Q 6x2 525"):
    return {
        "date": date,
        "sport": "running",
        "name": name,
        "steps": [{"type": "interval", "duration": "2:00", "pace": "5:25-5:30"}],
    }


def test_training_plan_builds_entries_and_reuses_identical_definition():
    plan = TrainingPlan({"name": "Block", "workouts": [_workout("2026-08-11"), _workout("2026-08-18")]})
    assert plan.name == "Block"
    assert len(plan.entries) == 2
    assert len(plan.unique_workouts()) == 1


def test_plan_rejects_watch_prefix_collision():
    first = _workout("2026-08-11", "123456789012345 A")
    second = _workout("2026-08-12", "123456789012345 B")
    with pytest.raises(ValueError, match="first 15"):
        TrainingPlan({"workouts": [first, second]})


def test_plan_rejects_same_name_with_different_definition():
    first = _workout("2026-08-11")
    second = _workout("2026-08-18")
    second["steps"][0]["duration"] = "3:00"
    plan = TrainingPlan({"workouts": [first, second]})
    with pytest.raises(ValueError, match="reused"):
        plan.unique_workouts()


def test_plan_rejects_non_running_sport():
    workout = _workout("2026-08-11")
    workout["sport"] = "other"
    with pytest.raises(ValueError, match="running workouts only"):
        TrainingPlan({"workouts": [workout]})
