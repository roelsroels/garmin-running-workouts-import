from datetime import date, datetime

import pytest

from garminworkouts.activities import ActivitySummary
from garminworkouts.state import AppState, Goal


def _goal():
    return Goal(
        goal_type="complete_distance",
        description="Complete 10 km",
        start_date=date(2030, 1, 8),
        target_distance_km=10,
    )


def _config():
    return {
        "name": "Block",
        "workouts": [
            {
                "date": "2030-01-08",
                "sport": "running",
                "name": "300108 Easy30",
                "steps": [{"type": "interval", "duration": "30:00"}],
            },
            {
                "date": "2030-01-10",
                "sport": "running",
                "name": "300110 Easy30",
                "steps": [{"type": "interval", "duration": "30:00"}],
            },
        ],
    }


def _activity(activity_id, activity_date):
    return ActivitySummary(
        str(activity_id),
        "Run",
        datetime.combine(activity_date, datetime.min.time()),
        "running",
    )


def test_state_persists_goal_plan_and_progress(tmp_path):
    with AppState(tmp_path / "state") as state:
        goal = state.save_goal(_goal())
        plan = state.save_plan(goal, _config(), tmp_path / "plan.yaml", "moderate", ["Reason"])
        state.activate_plan(plan.id)
        progress = state.refresh_progress(
            plan.id,
            [_activity(1, date(2030, 1, 8))],
            today=date(2030, 1, 11),
        )

        assert state.active_goal().description == "Complete 10 km"
        assert state.active_plan().name == "Block"
        assert [item["status"] for item in progress] == ["completed", "missed"]
        assert state.progress_summary(plan.id) == {
            "total": 2,
            "completed": 1,
            "missed": 1,
            "scheduled": 0,
            "remaining": 0,
        }


def test_new_active_goal_retires_previous_goal(tmp_path):
    with AppState(tmp_path / "state") as state:
        first = state.save_goal(_goal())
        second = state.save_goal(
            Goal(
                goal_type="consistency",
                description="Run three times each week",
                start_date=date(2030, 2, 1),
            )
        )

        assert first.id != second.id
        assert state.active_goal().id == second.id


def test_replacement_block_keeps_elapsed_progress_but_not_retired_future_dates(tmp_path):
    with AppState(tmp_path / "state") as state:
        goal = state.save_goal(_goal())
        original = state.save_plan(goal, _config(), tmp_path / "original.yaml", "moderate", [])
        state.refresh_progress(original.id, [_activity(1, date(2030, 1, 8))], today=date(2030, 1, 9))
        replacement_config = {
            "name": "Replacement",
            "workouts": [
                {
                    "date": "2030-01-12",
                    "sport": "running",
                    "name": "300112 Easy25",
                    "steps": [{"type": "interval", "duration": "25:00"}],
                }
            ],
        }
        replacement = state.save_plan(
            goal,
            replacement_config,
            tmp_path / "replacement.yaml",
            "moderate",
            [],
            supersedes_plan_id=original.id,
        )

        assert [(row["workout_date"], row["status"]) for row in state.block_progress(replacement.id)] == [
            ("2030-01-08", "completed"),
            ("2030-01-12", "scheduled"),
        ]
        assert state.block_progress_summary(replacement.id)["completed"] == 1


def test_goal_validation_rejects_missing_required_target():
    try:
        Goal(goal_type="target_time", description="Fast 10k", start_date=date.today())
    except ValueError as exc:
        assert "target distance" in str(exc)
    else:
        raise AssertionError("Goal validation should reject incomplete target-time goals")


def test_sustain_pace_goal_requires_a_duration():
    with pytest.raises(ValueError, match="target duration"):
        Goal(
            goal_type="sustain_pace",
            description="Hold a pace",
            start_date=date.today(),
            target_pace_seconds_per_km=360,
        )
