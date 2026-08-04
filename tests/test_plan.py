import json
from unittest.mock import patch

import pytest

from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.plan import PlanApplier, preview_plan


class FakeConnection:
    def __init__(self, existing=None, scheduled=None):
        self.existing = existing or []
        self.scheduled = scheduled or []
        self.saved = []
        self.updated = []
        self.scheduled_calls = []

    def list_workouts(self):
        return iter(self.existing)

    def save_workout(self, payload):
        self.saved.append(payload)
        return {"workoutId": 101}

    def update_workout(self, workout_id, payload):
        self.updated.append((workout_id, payload))

    def list_scheduled_workouts(self, year, month):
        return self.scheduled

    def schedule_workout(self, workout_id, date):
        self.scheduled_calls.append((workout_id, date))


def _plan():
    workout = {
        "sport": "running",
        "name": "W1Q 6x2 525",
        "steps": [{"type": "interval", "duration": "2:00", "pace": "5:25-5:30"}],
    }
    return TrainingPlan(
        {
            "name": "Two dates",
            "workouts": [dict(workout, date="2026-08-11"), dict(workout, date="2026-08-18")],
        }
    )


def test_preview_plan_has_no_connection_side_effects():
    preview = json.loads(preview_plan(_plan()))
    assert preview["mode"].startswith("preview-only")
    assert len(preview["workouts"]) == 2
    assert preview["workouts"][0]["payload"]["sportType"]["sportTypeKey"] == "running"


def test_apply_creates_once_and_schedules_both_dates():
    connection = FakeConnection()
    actions = PlanApplier(_plan(), connection).apply()
    assert len(connection.saved) == 1
    assert connection.scheduled_calls == [(101, "2026-08-11"), (101, "2026-08-18")]
    assert [action["action"] for action in actions] == ["created", "scheduled", "scheduled"]


def test_apply_updates_existing_and_skips_existing_schedule():
    existing = [
        {
            "workoutId": 7,
            "ownerId": 8,
            "workoutName": "W1Q 6x2 525",
            "description": "old",
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        }
    ]
    scheduled = [{"workoutId": 7, "date": "2026-08-11"}]
    connection = FakeConnection(existing, scheduled)
    actions = PlanApplier(_plan(), connection).apply()
    assert connection.updated[0][0] == 7
    assert connection.scheduled_calls == [(7, "2026-08-18")]
    assert [action["action"] for action in actions] == ["updated", "schedule-skipped", "scheduled"]


def test_apply_ignores_existing_non_running_workout_with_same_name():
    existing = [
        {
            "workoutId": 7,
            "ownerId": 8,
            "workoutName": "W1Q 6x2 525",
            "description": "other sport",
            "sportType": {"sportTypeId": 3, "sportTypeKey": "other"},
        }
    ]
    connection = FakeConnection(existing)
    actions = PlanApplier(_plan(), connection).apply(schedule=False)
    assert len(connection.saved) == 1
    assert not connection.updated
    assert actions[0]["action"] == "created"


def test_apply_ignores_duplicate_workout_names_outside_plan():
    unrelated = {
        "workoutName": "Benchmark Run",
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
    }
    existing = [dict(unrelated, workoutId=7), dict(unrelated, workoutId=8)]
    connection = FakeConnection(existing)

    actions = PlanApplier(_plan(), connection).apply(schedule=False)

    assert len(connection.saved) == 1
    assert actions[0]["action"] == "created"


def test_apply_rejects_duplicate_workout_name_used_by_plan():
    planned = {
        "workoutName": "W1Q 6x2 525",
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
    }
    existing = [dict(planned, workoutId=7), dict(planned, workoutId=8)]
    connection = FakeConnection(existing)

    with pytest.raises(ValueError, match="W1Q 6x2 525"):
        PlanApplier(_plan(), connection).apply(schedule=False)


def test_workout_id_is_extracted_from_nested_upload_response():
    response = {"data": {"workout": {"workoutId": 123}}}
    assert PlanApplier._workout_id_from_response(response) == 123


def test_created_workout_lookup_retries_for_api_visibility():
    created = {
        "workoutId": 123,
        "workoutName": "W1Q 6x2 525",
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
    }
    connection = FakeConnection()
    responses = iter([[], [created]])
    connection.list_workouts = lambda: iter(next(responses))

    with patch("garminworkouts.plan.time.sleep") as sleep:
        workout_id = PlanApplier(_plan(), connection)._find_created_workout_id("W1Q 6x2 525")

    assert workout_id == 123
    sleep.assert_called_once_with(1)
