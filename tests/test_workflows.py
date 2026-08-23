from datetime import date

import pytest

from garminworkouts.state import AppState
from garminworkouts.workflows import PlannerWorkflow


def test_workflow_conflict_choice_requires_removal_or_duplicate_acknowledgement():
    preview = {
        "summary": {
            "overlapping_calendar_entries": 2,
            "unresolved_calendar_entries": 0,
        }
    }

    with pytest.raises(ValueError, match="acknowledge"):
        PlannerWorkflow._validate_conflict_choice(preview, False, False, False)
    PlannerWorkflow._validate_conflict_choice(preview, False, False, True)
    PlannerWorkflow._validate_conflict_choice(preview, True, True, False)


def test_workflow_blocks_unresolved_cleanup_before_garmin_changes():
    preview = {
        "summary": {
            "overlapping_calendar_entries": 1,
            "unresolved_calendar_entries": 1,
        }
    }

    with pytest.raises(ValueError, match="no changes"):
        PlannerWorkflow._validate_conflict_choice(preview, True, False, False)


def test_workflow_stores_and_validates_private_one_off_draft(tmp_path):
    config = {
        "name": "One off",
        "workouts": [
            {
                "date": "2030-01-10",
                "sport": "running",
                "name": "300110 HR test",
                "steps": [{"type": "interval", "duration": "10:00", "heart_rate_max": 140}],
            }
        ],
    }
    with AppState(tmp_path / "state") as state:
        workflow = PlannerWorkflow(state, today=date(2030, 1, 1))
        draft_id = workflow.save_one_off_draft(config)
        stored = workflow.load_one_off_draft(draft_id)
        draft_path = state.data_dir / "web" / f"draft-{draft_id}.json"

    assert stored == config
    assert draft_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="Invalid draft"):
        workflow.load_one_off_draft("../escape")


def test_workflow_calendar_changes_keeps_elapsed_dates_out_of_removals():
    old = {
        "workouts": [
            {"date": "2030-01-01", "name": "Past", "steps": []},
            {"date": "2030-01-10", "name": "Old", "steps": []},
        ]
    }
    new = {"workouts": [{"date": "2030-01-10", "name": "New", "steps": [{"type": "interval"}]}]}

    changes = PlannerWorkflow.calendar_changes(old, new, today=date(2030, 1, 5))

    assert changes == ["2030-01-10: Old → New"]


def test_workflow_calendar_changes_only_removes_upcoming_mutable_workouts():
    old = {
        "workouts": [
            {"date": "2030-01-01", "name": "Missed", "steps": []},
            {"date": "2030-01-10", "name": "Completed early", "steps": []},
            {"date": "2030-01-12", "name": "Mutable", "steps": []},
        ]
    }
    progress = [
        {"workout_date": "2030-01-01", "workout_name": "Missed", "status": "missed"},
        {"workout_date": "2030-01-10", "workout_name": "Completed early", "status": "completed"},
        {"workout_date": "2030-01-12", "workout_name": "Mutable", "status": "scheduled"},
    ]

    changes = PlannerWorkflow.calendar_changes(
        old,
        {"workouts": []},
        today=date(2030, 1, 5),
        progress=progress,
    )

    assert changes == ["2030-01-12: remove Mutable"]


def test_workflow_calendar_changes_suppresses_nearest_five_rounding_no_op():
    old = {
        "workouts": [
            {
                "date": "2030-01-10",
                "sport": "running",
                "name": "300110 Easy41 HR",
                "description": "41 min easy; conversational",
                "steps": [
                    {"type": "warmup", "duration": "5:00"},
                    {"type": "interval", "duration": "31:00", "heart_rate_max": 140},
                    {"type": "cooldown", "duration": "5:00"},
                ],
            }
        ]
    }
    new = {
        "workouts": [
            {
                "date": "2030-01-10",
                "sport": "running",
                "name": "300110 Easy40 HR",
                "description": "40 min easy; conversational",
                "steps": [
                    {"type": "warmup", "duration": "5:00"},
                    {"type": "interval", "duration": "30:00", "heart_rate_max": 140},
                    {"type": "cooldown", "duration": "5:00"},
                ],
            }
        ]
    }

    assert PlannerWorkflow.calendar_changes(old, new, today=date(2030, 1, 5)) == []
