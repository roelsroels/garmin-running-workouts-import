from datetime import date

import pytest

from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.retire import PlanRetirement, ScheduledConflictCleanup


def _plan(entries, name="Old block"):
    return TrainingPlan(
        {
            "name": name,
            "workouts": [
                {
                    "date": workout_date,
                    "sport": "running",
                    "name": workout_name,
                    "steps": [{"type": "interval", "duration": "10:00"}],
                }
                for workout_date, workout_name in entries
            ],
        }
    )


def _workout(workout_id, name):
    return {
        "workoutId": workout_id,
        "ownerId": 8,
        "workoutName": name,
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
    }


def _cycling_workout(workout_id, name):
    return {
        "workoutId": workout_id,
        "ownerId": 8,
        "workoutName": name,
        "sportType": {"sportTypeId": 2, "sportTypeKey": "cycling"},
    }


class FakeConnection:
    def __init__(self, workouts=None, scheduled=None):
        self.workouts = workouts or []
        self.scheduled = scheduled or {"calendarItems": []}
        self.unscheduled = []
        self.deleted = []

    def list_workouts(self):
        return iter(self.workouts)

    def list_scheduled_workouts(self, year, month):
        return self.scheduled

    def unschedule_workout(self, scheduled_workout_id):
        self.unscheduled.append(scheduled_workout_id)

    def delete_workout(self, workout_id):
        self.deleted.append(workout_id)


def test_preview_and_apply_retains_past_workout_and_template_then_retires_future():
    plan = _plan([("2026-08-06", "260806 Easy"), ("2026-08-13", "260813 Quality")])
    connection = FakeConnection(
        workouts=[_workout(101, "260806 Easy"), _workout(102, "260813 Quality")],
        scheduled={
            "calendarItems": [
                {"id": 201, "workoutId": 101, "date": "2026-08-06"},
                {"id": 202, "workoutId": 102, "date": "2026-08-13"},
                {"id": 999, "date": "2026-08-13"},
            ]
        },
    )
    retirement = PlanRetirement(plan, connection, today=date(2026, 8, 10))

    preview = retirement.preview()
    actions = retirement.apply(preview)

    assert preview["summary"] == {
        "future_calendar_entries_to_unschedule": 1,
        "past_calendar_entries_retained": 1,
        "immutable_calendar_entries_retained": 0,
        "workout_templates_to_delete": 1,
        "protected_workout_templates": 0,
        "immutable_workout_templates": 1,
        "missing_workout_templates": 0,
    }
    assert connection.unscheduled == [202]
    assert connection.deleted == [102]
    assert [action["action"] for action in actions] == [
        "unscheduled",
        "deleted-workout-template",
    ]


def test_finished_plan_cleanup_deletes_past_templates_and_protects_reused_names():
    plan = _plan([("2026-08-06", "Shared Easy"), ("2026-08-09", "Old Long")])
    protected_plan = _plan([("2026-09-06", "Shared Easy")], name="New block")
    connection = FakeConnection(
        workouts=[_workout(101, "Shared Easy"), _workout(102, "Old Long")],
        scheduled={
            "calendarItems": [
                {"id": 201, "workoutId": 101, "date": "2026-08-06"},
                {"id": 202, "workoutId": 102, "date": "2026-08-09"},
            ]
        },
    )
    retirement = PlanRetirement(
        plan,
        connection,
        protected_plans=[protected_plan],
        today=date(2026, 8, 10),
        immutable_workouts={("2026-08-06", "Shared Easy"), ("2026-08-09", "Old Long")},
        delete_finished_templates=True,
    )

    preview = retirement.preview()
    actions = retirement.apply(preview)

    assert preview["summary"]["past_calendar_entries_retained"] == 2
    assert preview["summary"]["workout_templates_to_delete"] == 1
    assert preview["summary"]["protected_workout_templates"] == 1
    assert preview["summary"]["immutable_workout_templates"] == 0
    assert next(item for item in preview["workouts"] if item["name"] == "Shared Easy")["action"] == ("retain-protected")
    assert next(item for item in preview["workouts"] if item["name"] == "Old Long")["action"] == "delete"
    assert connection.unscheduled == []
    assert connection.deleted == [102]
    assert [action["action"] for action in actions] == ["deleted-workout-template"]
    assert any("finished plan" in warning for warning in preview["warnings"])


def test_retirement_keeps_future_completed_workout_immutable():
    plan = _plan([("2026-08-13", "Completed early"), ("2026-08-16", "Mutable")])
    connection = FakeConnection(
        workouts=[_workout(101, "Completed early"), _workout(102, "Mutable")],
        scheduled={
            "calendarItems": [
                {"id": 201, "workoutId": 101, "date": "2026-08-13"},
                {"id": 202, "workoutId": 102, "date": "2026-08-16"},
            ]
        },
    )
    retirement = PlanRetirement(
        plan,
        connection,
        today=date(2026, 8, 10),
        immutable_workouts={("2026-08-13", "Completed early")},
    )

    preview = retirement.preview()
    retirement.apply(preview)

    assert next(item for item in preview["calendar"] if item["name"] == "Completed early")["action"] == (
        "retain-immutable"
    )
    assert next(item for item in preview["workouts"] if item["name"] == "Completed early")["action"] == (
        "retain-immutable"
    )
    assert connection.unscheduled == [202]
    assert connection.deleted == [102]


def test_protected_plan_retains_reused_template_and_calendar_entry():
    old_plan = _plan([("2026-08-13", "Shared Quality"), ("2026-08-16", "Old Long")])
    protected_plan = _plan([("2026-08-13", "Shared Quality")], name="New block")
    connection = FakeConnection(
        workouts=[_workout(101, "Shared Quality"), _workout(102, "Old Long")],
        scheduled={
            "calendarItems": [
                {"id": 201, "workoutId": 101, "date": "2026-08-13"},
                {"id": 202, "workoutId": 102, "date": "2026-08-16"},
            ]
        },
    )
    retirement = PlanRetirement(
        old_plan,
        connection,
        protected_plans=[protected_plan],
        today=date(2026, 8, 10),
    )

    preview = retirement.preview()
    retirement.apply(preview)

    assert connection.unscheduled == [202]
    assert connection.deleted == [102]
    assert next(item for item in preview["calendar"] if item["name"] == "Shared Quality")["action"] == (
        "retain-protected"
    )


def test_apply_blocks_future_schedule_without_schedule_id():
    plan = _plan([("2026-08-13", "Quality")])
    connection = FakeConnection(
        workouts=[_workout(101, "Quality")],
        scheduled={"calendarItems": [{"workoutId": 101, "date": "2026-08-13"}]},
    )
    retirement = PlanRetirement(plan, connection, today=date(2026, 8, 10))
    preview = retirement.preview()

    assert preview["calendar"][0]["action"] == "unresolved-schedule-id"
    with pytest.raises(RuntimeError, match="schedule ID"):
        retirement.apply(preview)
    assert not connection.deleted


def test_missing_template_is_reported_without_deletion():
    plan = _plan([("2026-08-13", "Missing")])
    connection = FakeConnection()

    preview = PlanRetirement(plan, connection, today=date(2026, 8, 10)).preview()

    assert preview["summary"]["missing_workout_templates"] == 1
    assert preview["calendar"][0]["action"] == "unresolved-missing-template"


def test_duplicate_target_workout_names_are_rejected():
    plan = _plan([("2026-08-13", "Duplicate")])
    connection = FakeConnection(workouts=[_workout(101, "Duplicate"), _workout(102, "Duplicate")])

    with pytest.raises(ValueError, match="duplicate target"):
        PlanRetirement(plan, connection).preview()


def test_conflict_cleanup_finds_overlapping_remote_schedule_and_protects_replacement():
    replacement = _plan([("2026-08-13", "260813 New"), ("2026-08-16", "260816 New")], name="New")
    connection = FakeConnection(
        workouts=[
            _workout(101, "260813 Old"),
            _workout(102, "260813 New"),
            _workout(103, "Unrelated"),
        ],
        scheduled={
            "calendarItems": [
                {"id": 201, "workoutId": 101, "date": "2026-08-13"},
                {"id": 202, "workoutId": 102, "date": "2026-08-13"},
                {"id": 203, "workoutId": 103, "date": "2026-08-20"},
            ]
        },
    )
    cleanup = ScheduledConflictCleanup(replacement, connection, today=date(2026, 8, 10))

    preview = cleanup.preview()
    actions = cleanup.apply(preview, delete_templates=True)

    assert preview["summary"] == {
        "overlapping_calendar_entries": 1,
        "unresolved_calendar_entries": 0,
        "obsolete_template_candidates": 1,
    }
    assert connection.unscheduled == [201]
    assert connection.deleted == [101]
    assert [action["action"] for action in actions] == [
        "unscheduled-conflict",
        "deleted-conflicting-workout-template",
    ]


def test_conflict_cleanup_blocks_unresolved_schedule_without_changes():
    replacement = _plan([("2026-08-13", "260813 New")], name="New")
    connection = FakeConnection(
        workouts=[_workout(101, "260813 Old")],
        scheduled={"calendarItems": [{"workoutId": 101, "date": "2026-08-13"}]},
    )
    cleanup = ScheduledConflictCleanup(replacement, connection, today=date(2026, 8, 10))
    preview = cleanup.preview()

    with pytest.raises(RuntimeError, match="schedule ID"):
        cleanup.apply(preview, delete_templates=True)
    assert not connection.unscheduled
    assert not connection.deleted


def test_conflict_cleanup_never_targets_non_running_workouts():
    replacement = _plan([("2026-08-13", "260813 New")], name="New")
    connection = FakeConnection(
        workouts=[_cycling_workout(501, "Bike intervals")],
        scheduled={"calendarItems": [{"id": 601, "workoutId": 501, "date": "2026-08-13"}]},
    )

    preview = ScheduledConflictCleanup(replacement, connection, today=date(2026, 8, 10)).preview()

    assert preview["summary"]["overlapping_calendar_entries"] == 0


def test_conflict_cleanup_keeps_one_exact_schedule_and_removes_extra_duplicates():
    replacement = _plan([("2026-08-13", "260813 New")], name="New")
    connection = FakeConnection(
        workouts=[_workout(102, "260813 New")],
        scheduled={
            "calendarItems": [
                {"id": 201, "workoutId": 102, "date": "2026-08-13"},
                {"id": 202, "workoutId": 102, "date": "2026-08-13"},
            ]
        },
    )

    cleanup = ScheduledConflictCleanup(replacement, connection, today=date(2026, 8, 10))
    preview = cleanup.preview()
    cleanup.apply(preview, delete_templates=True)

    assert [item["scheduled_workout_id"] for item in preview["calendar"]] == [202]
    assert connection.unscheduled == [202]
    assert connection.deleted == []
