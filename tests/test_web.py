from datetime import date, timedelta

from werkzeug.datastructures import MultiDict

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

    for path in ("/goal", "/calendar", "/cleanup", "/workouts/new", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"Running Planner" in response.data


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
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
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
    assert b"300110 Easy30" in calendar.data
    assert b"Inspect Garmin schedule" in cleanup.data
    assert b"A reviewable reason" in review.data
