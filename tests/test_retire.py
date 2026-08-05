from datetime import date

import pytest

from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.retire import PlanRetirement


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


def test_preview_and_apply_retains_past_unschedules_future_then_deletes_templates():
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
        "workout_templates_to_delete": 2,
        "protected_workout_templates": 0,
        "missing_workout_templates": 0,
    }
    assert connection.unscheduled == [202]
    assert connection.deleted == [101, 102]
    assert [action["action"] for action in actions] == [
        "unscheduled",
        "deleted-workout-template",
        "deleted-workout-template",
    ]


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
