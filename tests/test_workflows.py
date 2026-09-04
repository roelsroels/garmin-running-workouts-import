from datetime import date
from unittest.mock import MagicMock

import pytest

from garminworkouts.state import AppState, Goal
from garminworkouts.workflows import PlannerWorkflow


def test_workflow_web_login_preserves_normal_flow_and_defers_only_mfa(tmp_path, monkeypatch):
    ordinary = MagicMock(mfa_required=False)
    mfa = MagicMock(mfa_required=True)
    clients = iter((ordinary, mfa))
    constructor = MagicMock(side_effect=lambda *_args, **_kwargs: next(clients))
    monkeypatch.setattr("garminworkouts.workflows.GarminClient", constructor)

    with AppState(tmp_path / "state") as state:
        workflow = PlannerWorkflow(state)
        assert workflow.begin_garmin_login("runner@example.test", "password") is None
        assert state.get_setting("garmin_username") == "runner@example.test"
        assert workflow.begin_garmin_login("mfa@example.test", "password") is mfa
        assert state.get_setting("garmin_username") == "runner@example.test"

    ordinary.open.assert_called_once_with()
    ordinary.list_recent_activities.assert_called_once_with(1)
    ordinary.close.assert_called_once_with()
    mfa.open.assert_called_once_with()
    mfa.list_recent_activities.assert_not_called()
    mfa.close.assert_not_called()
    assert all(call.kwargs["defer_mfa"] for call in constructor.call_args_list)


def test_workflow_completes_mfa_before_recording_connection(tmp_path):
    connection = MagicMock(mfa_required=False)

    with AppState(tmp_path / "state") as state:
        workflow = PlannerWorkflow(state)
        workflow.complete_garmin_login("runner@example.test", connection, "123456")
        assert state.get_setting("garmin_username") == "runner@example.test"
        assert state.get_setting("garmin_token_store") == str(state.tokens_dir)

    connection.resume_mfa.assert_called_once_with("123456")
    connection.list_recent_activities.assert_called_once_with(1)
    connection.close.assert_called_once_with()


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


def test_workflow_requires_remaining_reassessment_when_an_active_plan_exists(tmp_path):
    config = {
        "name": "Active",
        "workouts": [
            {
                "date": "2030-01-10",
                "sport": "running",
                "name": "300110 Easy30",
                "steps": [{"type": "interval", "duration": "30:00"}],
            }
        ],
    }
    with AppState(tmp_path / "state") as state:
        goal = state.save_goal(
            Goal(
                goal_type="complete_distance",
                description="Complete ten kilometres",
                start_date=date(2030, 1, 10),
                target_distance_km=10,
            )
        )
        active = state.save_plan(goal, config, state.plans_dir / "active.yaml", "moderate", ())
        state.activate_plan(active.id)

        with pytest.raises(ValueError, match="reassess its remaining workouts"):
            PlannerWorkflow(state, today=date(2030, 1, 5)).generate_plan()


def test_workflow_generates_next_block_after_refreshing_finished_plan(tmp_path):
    config = {
        "name": "Finished",
        "workouts": [
            {
                "date": "2030-01-03",
                "sport": "running",
                "name": "300103 Easy30",
                "steps": [{"type": "interval", "duration": "30:00"}],
            }
        ],
    }
    with AppState(tmp_path / "state") as state:
        goal = state.save_goal(
            Goal(
                goal_type="complete_distance",
                description="Complete ten kilometres",
                start_date=date(2030, 1, 3),
                target_distance_km=10,
            )
        )
        active = state.save_plan(goal, config, state.plans_dir / "finished.yaml", "moderate", ())
        state.activate_plan(active.id)
        state.refresh_progress(active.id, [], today=date(2030, 1, 5))
        workflow = PlannerWorkflow(state, today=date(2030, 1, 5))
        connection = MagicMock()
        managed = MagicMock()
        managed.__enter__.return_value = connection
        workflow.garmin_client = MagicMock(return_value=managed)
        workflow.refresh = MagicMock()
        workflow.fetch_recent_history = MagicMock(return_value=[])
        workflow._prepare_fit_analysis = MagicMock(return_value={"activities": [], "failures": []})

        record, changes = workflow.generate_next_block()

        assert record.supersedes_plan_id == active.id
        assert record.start_date >= date(2030, 1, 5)
        assert changes and all(": add " in change for change in changes)
        assert record.config["metadata"]["continued_from_plan_id"] == active.id
        workflow.refresh.assert_called_once_with(connection=connection)
        workflow.fetch_recent_history.assert_called_once_with(connection=connection)
        workflow._prepare_fit_analysis.assert_called_once_with([], connection)


def test_workflow_next_block_requires_an_active_plan(tmp_path):
    with AppState(tmp_path / "state") as state:
        state.save_goal(
            Goal(
                goal_type="complete_distance",
                description="Complete ten kilometres",
                start_date=date(2030, 1, 3),
                target_distance_km=10,
            )
        )
        with pytest.raises(ValueError, match="active goal and finished plan"):
            PlannerWorkflow(state, today=date(2030, 1, 5)).generate_next_block()


def test_workflow_rejects_stale_replacement_with_immutable_dates_before_garmin(tmp_path):
    old_config = {
        "name": "Old",
        "workouts": [
            {
                "date": "2030-01-01",
                "sport": "running",
                "name": "300101 Easy30",
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
    with AppState(tmp_path / "state") as state:
        goal = state.save_goal(
            Goal(
                goal_type="complete_distance",
                description="Complete ten kilometres",
                start_date=date(2030, 1, 1),
                target_distance_km=10,
            )
        )
        active = state.save_plan(goal, old_config, state.plans_dir / "old.yaml", "moderate", ())
        state.activate_plan(active.id)
        state.refresh_progress(active.id, [], today=date(2030, 1, 5))
        stale = state.save_plan(
            goal,
            old_config,
            state.plans_dir / "stale.yaml",
            "moderate",
            (),
            supersedes_plan_id=active.id,
        )

        with pytest.raises(ValueError, match="past, completed, or missed"):
            PlannerWorkflow(state, today=date(2030, 1, 5)).inspect_plan_conflicts(stale)


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
