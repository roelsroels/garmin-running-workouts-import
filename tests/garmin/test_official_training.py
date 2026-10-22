import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from garminworkouts.garmin.official_training import OfficialObjectRegistry, OfficialTrainingPublisher
from garminworkouts.garmin.starterclient import GarminStarterNotFoundError
from garminworkouts.models.training_plan import TrainingPlan


def _plan(*workouts):
    return TrainingPlan(
        {
            "name": "Official block",
            "workouts": [
                {
                    "date": workout_date,
                    "sport": "running",
                    "name": name,
                    "description": "Easy and controlled",
                    "steps": [{"type": "interval", "duration": "30:00", "heart_rate_max": 140}],
                }
                for workout_date, name in workouts
            ],
        }
    )


def test_registry_is_private_atomic_and_bound_to_connected_user(tmp_path):
    registry = OfficialObjectRegistry(tmp_path / "starter-api", "runner-one")
    registry.record_workout("Easy", 42, "hash")
    registry.record_schedule("2030-01-06", "Easy", 84, 42, 7)

    assert registry.path.stat().st_mode & 0o777 == 0o600
    assert registry.workout("Easy")["workout_id"] == 42
    assert registry.schedule("2030-01-06", "Easy")["schedule_id"] == 84
    assert not registry.path.with_suffix(".tmp").exists()
    with pytest.raises(ValueError, match="another connected user"):
        OfficialObjectRegistry(tmp_path / "starter-api", "runner-two")


def test_official_publisher_creates_records_and_idempotently_updates(tmp_path):
    plan = _plan(("2030-01-06", "Easy30"))
    connection = MagicMock()
    connection.save_workout.return_value = {"workoutId": 42}
    connection.schedule_workout.return_value = {"scheduleId": 84}
    connection.list_scheduled_workouts.return_value = []
    registry = OfficialObjectRegistry(tmp_path / "starter-api", "runner")

    first = OfficialTrainingPublisher(plan, connection, registry, 7, today=date(2030, 1, 1)).apply()

    assert [item["action"] for item in first] == ["created", "scheduled"]
    assert registry.workout("Easy30")["workout_id"] == 42
    assert registry.schedule("2030-01-06", "Easy30")["schedule_id"] == 84
    assert json.loads(registry.path.read_text())["schedules"]["2030-01-06|Easy30"]["plan_id"] == "7"

    connection.reset_mock()
    connection.get_workout.return_value = {"workoutId": 42, "ownerId": 9, "workoutName": "Easy30"}
    connection.list_scheduled_workouts.return_value = [{"scheduleId": 84, "workoutId": 42, "date": "2030-01-06"}]
    second = OfficialTrainingPublisher(plan, connection, registry, 7, today=date(2030, 1, 1)).apply()

    assert [item["action"] for item in second] == ["updated", "schedule-skipped"]
    update = connection.update_workout.call_args.args[1]
    assert update["workoutId"] == 42
    assert update["ownerId"] == 9
    connection.save_workout.assert_not_called()
    connection.schedule_workout.assert_not_called()


def test_official_publisher_recreates_stale_recorded_workout(tmp_path):
    plan = _plan(("2030-01-06", "Easy30"))
    registry = OfficialObjectRegistry(tmp_path / "starter-api", "runner")
    registry.record_workout("Easy30", 10, "old")
    connection = MagicMock()
    connection.get_workout.side_effect = GarminStarterNotFoundError("gone")
    connection.save_workout.return_value = {"workoutId": 42}
    connection.schedule_workout.return_value = {"scheduleId": 84}
    connection.list_scheduled_workouts.return_value = []

    OfficialTrainingPublisher(plan, connection, registry, 7, today=date(2030, 1, 1)).apply()

    assert registry.workout("Easy30")["workout_id"] == 42


def test_official_conflict_cleanup_only_offers_owned_templates_for_deletion(tmp_path):
    plan = _plan(("2030-01-06", "Planned"))
    registry = OfficialObjectRegistry(tmp_path / "starter-api", "runner")
    registry.record_workout("Owned old workout", 42, "hash")
    connection = MagicMock()
    connection.list_scheduled_workouts.return_value = [
        {"scheduleId": 80, "workoutId": 42, "date": "2030-01-06"},
        {"scheduleId": 81, "workoutId": 99, "date": "2030-01-06"},
    ]
    connection.get_workout.side_effect = [
        {"workoutId": 42, "workoutName": "Owned old workout"},
        {"workoutId": 99, "workoutName": "Coach workout"},
    ]
    publisher = OfficialTrainingPublisher(plan, connection, registry, 7, today=date(2030, 1, 1))

    preview = publisher.preview_conflicts()
    actions = publisher.apply_conflicts(preview, delete_templates=True)

    assert preview["summary"] == {
        "overlapping_calendar_entries": 2,
        "unresolved_calendar_entries": 0,
        "obsolete_template_candidates": 1,
    }
    assert [item["workout_id"] for item in preview["templates"]] == [42]
    assert connection.unschedule_workout.call_count == 2
    connection.delete_workout.assert_called_once_with(42)
    assert all(item.get("workout_id") != 99 for item in actions if item["action"].startswith("deleted"))


def test_official_preview_does_not_adopt_untracked_exact_workout(tmp_path):
    plan = _plan(("2030-01-06", "Planned"))
    registry = OfficialObjectRegistry(tmp_path / "starter-api", "runner")
    connection = MagicMock()
    connection.list_scheduled_workouts.return_value = [
        {"scheduleId": 80, "workoutId": 99, "date": "2030-01-06"},
    ]
    connection.get_workout.return_value = {"workoutId": 99, "workoutName": "Planned"}

    preview = OfficialTrainingPublisher(plan, connection, registry, 7, today=date(2030, 1, 1)).preview_conflicts()

    assert preview["summary"]["overlapping_calendar_entries"] == 1
    assert preview["calendar"][0]["template_can_be_deleted"] is False


def test_official_finished_plan_retirement_keeps_past_calendar_but_deletes_owned_template(tmp_path):
    old_plan = _plan(("2029-12-20", "Old easy"))
    new_plan = _plan(("2030-01-06", "New easy"))
    registry = OfficialObjectRegistry(tmp_path / "starter-api", "runner")
    registry.record_workout("Old easy", 42, "hash")
    registry.record_schedule("2029-12-20", "Old easy", 84, 42, 2)
    connection = MagicMock()
    publisher = OfficialTrainingPublisher(new_plan, connection, registry, 3, today=date(2030, 1, 1))

    actions = publisher.retire(old_plan, [2], protected_plan=new_plan, delete_finished_templates=True)

    connection.unschedule_workout.assert_not_called()
    connection.delete_workout.assert_called_once_with(42)
    assert actions == [{"action": "deleted-workout-template", "name": "Old easy", "workout_id": 42}]
