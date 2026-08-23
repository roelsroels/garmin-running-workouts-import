import sqlite3
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


def _activity(activity_id, activity_date, execution_score=None, execution_score_checked=False):
    return ActivitySummary(
        str(activity_id),
        "Run",
        datetime.combine(activity_date, datetime.min.time()),
        "running",
        execution_score=execution_score,
        execution_score_checked=execution_score_checked,
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


def test_state_persists_execution_score_and_checked_state(tmp_path):
    with AppState(tmp_path / "state") as state:
        goal = state.save_goal(_goal())
        plan = state.save_plan(goal, _config(), tmp_path / "plan.yaml", "moderate", ["Reason"])
        progress = state.refresh_progress(
            plan.id,
            [_activity(1, date(2030, 1, 8), execution_score=88, execution_score_checked=True)],
            today=date(2030, 1, 9),
        )

        assert progress[0]["execution_score"] == 88
        assert progress[0]["execution_score_checked_at"] is not None


def test_state_migrates_existing_progress_table_for_execution_scores(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = sqlite3.connect(state_dir / "state.sqlite3")
    database.execute(
        """
        CREATE TABLE plan_progress (
            plan_id INTEGER NOT NULL,
            workout_date TEXT NOT NULL,
            workout_name TEXT NOT NULL,
            status TEXT NOT NULL,
            activity_id TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(plan_id, workout_date, workout_name)
        )
        """
    )
    database.commit()
    database.close()

    with AppState(state_dir) as state:
        columns = {row["name"] for row in state.connection.execute("PRAGMA table_info(plan_progress)")}

    assert {"execution_score", "execution_score_checked_at"} <= columns


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


def test_goal_validates_and_normalizes_heart_rate_targets():
    goal = Goal(
        goal_type="endurance",
        description="Build endurance",
        start_date=date.today(),
        heart_rate_targets={
            "easy": {"heart_rate_max": "140"},
            "long": {"heart_rate": "120-145"},
            "quality": {"heart_rate_zone": 4},
        },
        quality_target_preference="heart_rate",
    )

    assert goal.heart_rate_targets == {
        "easy": {"heart_rate_max": 140},
        "long": {"heart_rate": [120, 145]},
        "quality": {"heart_rate_zone": 4},
    }


def test_heart_rate_quality_preference_requires_quality_target():
    with pytest.raises(ValueError, match="quality heart-rate target"):
        Goal(
            goal_type="endurance",
            description="Build endurance",
            start_date=date.today(),
            quality_target_preference="heart_rate",
        )
