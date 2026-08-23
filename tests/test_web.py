from datetime import date, datetime, timedelta

from flask import render_template
from werkzeug.datastructures import MultiDict

from garminworkouts.activities import ActivitySummary
from garminworkouts.state import AppState, Goal
from garminworkouts.web import create_app


def _app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path / "state"),
            "SECRET_KEY": "test-secret-key",
            "SESSION_COOKIE_SECURE": False,
        }
    )


def _csrf(client):
    with client.session_transaction() as session:
        session["csrf_token"] = "test-csrf"
    return "test-csrf"


def test_web_dashboard_goal_calendar_and_settings_render(tmp_path):
    client = _app(tmp_path).test_client()

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"Garmin training scheduler &amp; importer" in dashboard.data
    assert b'rel="icon" href="/static/favicon.svg" type="image/svg+xml"' in dashboard.data
    assert b">roels</a>" in dashboard.data
    assert b"https://github.com/roelsroels/garmin-running-workouts-import" in dashboard.data
    assert b"https://cdnjs.buymeacoffee.com/1.0.0/button.prod.min.js" in dashboard.data
    assert b'data-text="Buy me a beer"' in dashboard.data

    for path in ("/goal", "/calendar", "/cleanup", "/workouts/new", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"Running Planner" in response.data


def test_web_favicon_is_served_as_svg(tmp_path):
    response = _app(tmp_path).test_client().get("/static/favicon.svg")

    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert b'viewBox="0 0 64 64"' in response.data


def test_web_rejects_post_without_csrf(tmp_path):
    response = _app(tmp_path).test_client().post("/progress/refresh")

    assert response.status_code == 400


def test_web_goal_form_saves_mixed_heart_rate_targets(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    csrf = _csrf(client)
    start = date.today() + timedelta(days=2)
    response = client.post(
        "/goal",
        data=MultiDict(
            [
                ("csrf_token", csrf),
                ("goal_type", "sustain_pace"),
                ("description", "Hold a controlled pace"),
                ("target_pace", "5:20"),
                ("target_duration_minutes", "20"),
                ("start_date", start.isoformat()),
                ("plan_weeks", "4"),
                ("runs_per_week", "3"),
                ("available_days", "1"),
                ("available_days", "3"),
                ("available_days", "6"),
                ("long_run_day", "6"),
                ("max_session_minutes", "90"),
                ("baseline_long_run_km", "10.3"),
                ("hr_warmup_type", "max"),
                ("hr_warmup_value", "120"),
                ("hr_easy_type", "range"),
                ("hr_easy_value", "120-140"),
                ("hr_long_type", "zone"),
                ("hr_long_value", "2"),
                ("hr_quality_type", "max"),
                ("hr_quality_value", "165"),
                ("hr_recovery_type", "max"),
                ("hr_recovery_value", "135"),
                ("quality_target_preference", "heart_rate"),
                ("constraints", "No consecutive running days"),
            ]
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Hold a controlled pace" in response.data
    with AppState(app.config["DATA_DIR"]) as state:
        goal = state.active_goal()
    assert goal.target_pace_seconds_per_km == 320
    assert goal.heart_rate_targets == {
        "warmup": {"heart_rate_max": 120},
        "easy": {"heart_rate": [120, 140]},
        "long": {"heart_rate_zone": 2},
        "quality": {"heart_rate_max": 165},
        "recovery": {"heart_rate_max": 135},
    }
    assert goal.quality_target_preference == "heart_rate"


def test_web_one_off_builder_creates_reviewable_local_draft(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    csrf = _csrf(client)
    workout_date = date.today() + timedelta(days=1)

    response = client.post(
        "/workouts/new",
        data=MultiDict(
            [
                ("csrf_token", csrf),
                ("workout_date", workout_date.isoformat()),
                ("name", f"{workout_date:%y%m%d} HR Test"),
                ("description", "Two HR caps"),
                ("step_type", "warmup"),
                ("duration", "10:00"),
                ("target_type", "max"),
                ("target_value", "120"),
                ("step_type", "interval"),
                ("duration", "15:00"),
                ("target_type", "max"),
                ("target_value", "140"),
            ]
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Two HR caps" in response.data
    assert b"120 bpm" in response.data
    assert b"140 bpm" in response.data
    assert b"Inspect Garmin schedule" in response.data


def test_security_headers_are_present(tmp_path):
    response = _app(tmp_path).test_client().get("/")

    assert response.headers["X-Frame-Options"] == "DENY"
    policy = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in policy
    assert "script-src 'self' https://cdnjs.buymeacoffee.com" in policy
    assert "https://fonts.googleapis.com" in policy
    assert "font-src https://fonts.gstatic.com" in policy
    assert "'unsafe-inline'" not in policy
    assert response.headers["Cache-Control"] == "no-store"


def test_active_dashboard_calendar_cleanup_and_plan_review_render(tmp_path):
    app = _app(tmp_path)
    config = {
        "name": "Rendered block",
        "metadata": {"goal": {}, "baseline": {}},
        "workouts": [
            {
                "date": "2030-01-10",
                "sport": "running",
                "name": "300110 Easy30",
                "description": "30 minutes easy",
                "steps": [{"type": "interval", "duration": "30:00"}],
            }
        ],
    }
    with AppState(app.config["DATA_DIR"]) as state:
        goal = state.save_goal(
            Goal(
                goal_type="complete_distance",
                description="Complete ten kilometres",
                start_date=date(2030, 1, 10),
                target_distance_km=10,
            )
        )
        record = state.save_plan(
            goal,
            config,
            state.plans_dir / "rendered.yaml",
            "moderate",
            ("A reviewable reason",),
        )
        state.activate_plan(record.id)

    client = app.test_client()
    dashboard = client.get("/")
    calendar = client.get("/calendar")
    cleanup = client.get("/cleanup")
    review = client.get(f"/plans/{record.id}")

    assert b"Complete ten kilometres" in dashboard.data
    assert b"Reassess remaining plan" in dashboard.data
    assert b"Generate a new proposal" not in dashboard.data
    assert b"300110 Easy30" in calendar.data
    assert b"Inspect Garmin schedule" in cleanup.data
    assert b"A reviewable reason" in review.data


def test_proposal_review_separates_calendar_history_from_five_upcoming_workouts(tmp_path):
    app = _app(tmp_path)
    today = date.today()

    def workout(offset, name):
        workout_date = today + timedelta(days=offset)
        return {
            "date": workout_date.isoformat(),
            "sport": "running",
            "name": name,
            "description": "Controlled running",
            "steps": [{"type": "interval", "duration": "30:00"}],
        }

    history_and_future = [
        workout(-2, "Completed before today"),
        workout(-1, "Missed before today"),
        workout(0, "Completed 12 km today"),
        *(workout(offset, f"Upcoming {offset}") for offset in range(1, 6)),
    ]
    old_config = {"name": "Active block", "metadata": {}, "workouts": history_and_future}
    proposal_config = {
        "name": "Remaining block",
        "metadata": {},
        "workouts": history_and_future[3:],
    }
    activities = [
        ActivitySummary(
            "completed-before",
            "Completed before today",
            datetime.combine(today - timedelta(days=2), datetime.min.time()),
            "running",
        ),
        ActivitySummary(
            "completed-today",
            "Completed 12 km today",
            datetime.combine(today, datetime.min.time()),
            "running",
            distance_m=12000,
        ),
    ]
    with AppState(app.config["DATA_DIR"]) as state:
        goal = state.save_goal(
            Goal(
                goal_type="complete_distance",
                description="Complete ten kilometres",
                start_date=today - timedelta(days=2),
                target_distance_km=10,
            )
        )
        active = state.save_plan(goal, old_config, state.plans_dir / "active.yaml", "high", ())
        state.activate_plan(active.id)
        state.refresh_progress(active.id, activities, today=today)
        proposal = state.save_plan(
            goal,
            proposal_config,
            state.plans_dir / "proposal.yaml",
            "high",
            ("Completed sessions are evidence only.",),
            supersedes_plan_id=active.id,
        )

    response = app.test_client().get(f"/plans/{proposal.id}")

    assert response.status_code == 200
    assert b"3 completed or missed days" in response.data
    assert b"5 upcoming scheduled days" in response.data
    assert b"Completed 12 km today" in response.data
    assert b"status-completed is-finished" in response.data
    assert b"status-missed is-finished" in response.data
    assert b"Today \xc2\xb7 scheduled" not in response.data


def test_calendar_subdues_finished_rows_and_marks_the_next_action(tmp_path):
    with _app(tmp_path).test_request_context("/calendar"):
        rendered = render_template(
            "calendar.html",
            plan=None,
            today="2030-01-14",
            rows=[
                {"date": "2030-01-10", "name": "Finished", "description": "Done", "status": "completed"},
                {"date": "2030-01-12", "name": "Skipped", "description": "Missed", "status": "missed"},
                {
                    "date": "2030-01-13",
                    "name": "Missed without refresh",
                    "description": "No activity recorded",
                    "status": "missed",
                    "status_label": "Missed · needs refresh",
                    "inferred_missed": True,
                },
                {"date": "2030-01-14", "name": "Next", "description": "Scheduled", "status": "scheduled"},
                {"date": "2030-01-16", "name": "Later", "description": "Scheduled", "status": "scheduled"},
            ],
        )

    assert rendered.count("is-finished") == 3
    assert rendered.count("is-next-action") == 1
    assert "Missed · needs refresh" in rendered
    assert "is-inferred-missed" in rendered
    assert "Today · scheduled" in rendered
    assert 'is-next-action"><time>2030-01-14' in rendered


def test_calendar_infers_missed_for_an_elapsed_unrefreshed_workout(tmp_path):
    app = _app(tmp_path)
    missed_date = date.today() - timedelta(days=1)
    config = {
        "name": "Missed run block",
        "metadata": {"goal": {}, "baseline": {}},
        "workouts": [
            {
                "date": missed_date.isoformat(),
                "sport": "running",
                "name": "Elapsed easy run",
                "description": "30 minutes easy",
                "steps": [{"type": "interval", "duration": "30:00"}],
            }
        ],
    }
    with AppState(app.config["DATA_DIR"]) as state:
        goal = state.save_goal(
            Goal(
                goal_type="complete_distance",
                description="Complete ten kilometres",
                start_date=missed_date,
                target_distance_km=10,
            )
        )
        record = state.save_plan(goal, config, state.plans_dir / "missed.yaml", "moderate", ("A reason",))
        state.activate_plan(record.id)

    response = app.test_client().get("/calendar")

    assert response.status_code == 200
    assert b"Missed \xc2\xb7 needs refresh" in response.data
    assert b"status-missed is-finished is-inferred-missed" in response.data


def test_calendar_shows_execution_score_for_completed_run(tmp_path):
    app = _app(tmp_path)
    workout_date = date.today()
    config = {
        "name": "Scored run block",
        "metadata": {"goal": {}, "baseline": {}},
        "workouts": [
            {
                "date": workout_date.isoformat(),
                "sport": "running",
                "name": "Scored intervals",
                "description": "Controlled intervals",
                "steps": [{"type": "interval", "duration": "30:00", "heart_rate_max": 150}],
            }
        ],
    }
    activity = ActivitySummary(
        "42",
        "Scored intervals",
        datetime.combine(workout_date, datetime.min.time()),
        "running",
        execution_score=92,
        execution_score_checked=True,
    )
    with AppState(app.config["DATA_DIR"]) as state:
        goal = state.save_goal(
            Goal(
                goal_type="complete_distance",
                description="Complete ten kilometres",
                start_date=workout_date,
                target_distance_km=10,
            )
        )
        record = state.save_plan(goal, config, state.plans_dir / "scored.yaml", "moderate", ("A reason",))
        state.activate_plan(record.id)
        state.refresh_progress(record.id, [activity], today=workout_date)

    response = app.test_client().get("/calendar")

    assert response.status_code == 200
    assert b"Execution score 92%" in response.data
    assert b"execution-score good" in response.data
