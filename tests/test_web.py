from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, Mock

import pytest
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
            "APP_VERSION": "1.2.1-test",
            "APP_BRANCH": "test-branch",
        }
    )


def _csrf(client):
    with client.session_transaction() as session:
        session["csrf_token"] = "test-csrf"
    return "test-csrf"


def _save_claude(client, **overrides):
    return client.post(
        "/settings/llm",
        data={
            "csrf_token": _csrf(client),
            "llm_provider": "anthropic",
            **overrides,
        },
        follow_redirects=True,
    )


def test_claude_settings_key_stays_out_of_database_cookies_and_html(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    network = Mock(side_effect=AssertionError("Saving must not call the provider"))
    monkeypatch.setattr("garminworkouts.llm.urllib.request.build_opener", network)
    app = _app(tmp_path)
    client = app.test_client()
    secret = "test-only-claude-secret"

    response = _save_claude(client, llm_api_key=secret)

    assert response.status_code == 200
    assert b"Claude (Anthropic)" in response.data
    assert b'type="password" name="llm_api_key"' in response.data
    assert b"A temporary key is available" in response.data
    assert b"button.prod.min.js" not in response.data  # No third-party script on credential forms.
    assert secret.encode() not in response.data
    assert secret not in str(response.headers)
    with client.session_transaction() as session:
        assert secret not in str(dict(session))
        assert "llm_key_token" in session
    with AppState(app.config["DATA_DIR"]) as state:
        assert state.get_setting("llm_provider") == "anthropic"
        assert state.get_setting("llm_model") == "claude-haiku-4-5-20251001"
        assert secret not in "\n".join(state.connection.iterdump())
    assert b"No API key is available" in app.test_client().get("/settings").data
    assert b"No API key is available" in _app(tmp_path).test_client().get("/settings").data
    network.assert_not_called()


def test_claude_key_is_used_only_for_explicit_explanation_and_plan_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = (
        b'{"content":[{"type":"text","text":"Hold your training load steady."}],"stop_reason":"end_turn"}'
    )
    monkeypatch.setattr("garminworkouts.llm.urllib.request.build_opener", lambda *args: opener)
    app = _app(tmp_path)
    client = app.test_client()
    record, progress = _seed_calendar_for_export(app)
    assert _save_claude(client, llm_api_key="test-only-key").status_code == 200
    opener.open.assert_not_called()

    response = client.post(f"/plans/{record.id}/explain", data={"csrf_token": _csrf(client)})

    assert response.status_code == 302
    assert opener.open.call_args.args[0].get_header("X-api-key") == "test-only-key"
    with AppState(app.config["DATA_DIR"]) as state:
        from garminworkouts.workflows import PlannerWorkflow

        assert PlannerWorkflow(state).load_plan_explanation(record.id) == "Hold your training load steady."
        assert state.plan(record.id).config == record.config
        assert state.progress(record.id) == progress
        assert "test-only-key" not in "\n".join(state.connection.iterdump())
    other_client = app.test_client()
    assert other_client.post(f"/plans/{record.id}/explain", data={"csrf_token": _csrf(other_client)}).status_code == 400
    assert opener.open.call_count == 1


def test_llm_environment_key_blank_keep_replace_clear_and_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-test-key")
    app = _app(tmp_path)
    client = app.test_client()
    assert b"service environment" in _save_claude(client).data
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert _save_claude(client, llm_api_key="test-key-one").status_code == 200
    assert b"A temporary key is available" in _save_claude(client, llm_api_key="").data
    with client.session_transaction() as session:
        first_token = session["llm_key_token"]
    assert _save_claude(client, llm_api_key="test-key-two").status_code == 200
    with client.session_transaction() as session:
        assert session["llm_key_token"] != first_token
    assert b"No API key is available" in _save_claude(client, clear_llm_key="1").data
    assert _save_claude(client).status_code == 400
    assert _save_claude(client, llm_api_key="test-key-three").status_code == 200
    assert _save_claude(client, llm_provider="none").status_code == 200
    with client.session_transaction() as session:
        assert "llm_key_token" not in session


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"llm_api_key": "test\nkey"},
        {"llm_api_key": "x" * 4097},
        {"llm_provider": "unknown"},
        {"llm_api_key_env": "bad variable", "llm_api_key": "test-key"},
        {"llm_provider": "openai-compatible", "llm_api_key": "test-key"},
        {
            "llm_provider": "openai-compatible",
            "llm_model": "model",
            "llm_base_url": "http://unsafe.invalid",
            "llm_api_key": "test-key",
        },
    ],
)
def test_invalid_llm_settings_do_not_enable_provider(tmp_path, monkeypatch, overrides):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = _app(tmp_path)
    response = _save_claude(app.test_client(), **overrides)
    assert response.status_code == 400
    with AppState(app.config["DATA_DIR"]) as state:
        assert state.get_setting("llm_provider", "none") == "none"


def test_changing_provider_never_reuses_previous_temporary_key(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNNING_PLANNER_LLM_API_KEY", raising=False)
    app = _app(tmp_path)
    client = app.test_client()
    assert _save_claude(client, llm_api_key="claude-only-key").status_code == 200
    response = _save_claude(client, llm_provider="openai-compatible", llm_model="custom-model")
    assert response.status_code == 400
    assert b"claude-only-key" not in response.data


def test_llm_settings_requires_csrf_even_for_keys(tmp_path):
    client = _app(tmp_path).test_client()
    response = client.post("/settings/llm", data={"llm_provider": "anthropic", "llm_api_key": "test-key"})
    assert response.status_code == 400
    assert b"test-key" not in response.data


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
    assert b'aria-label="Running version 1.2.1-test on branch test-branch"' in dashboard.data
    assert b">v1.2.1-test</code>" in dashboard.data
    assert b">test-branch</code>" in dashboard.data
    assert b'id="wait-overlay" role="status" aria-live="polite"' in dashboard.data
    assert b'class="running-puppet"' in dashboard.data
    assert b'data-loading-message="Building your first proposal' in dashboard.data

    for path in ("/goal", "/calendar", "/cleanup", "/workouts/new", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"Running Planner" in response.data


def test_web_wait_state_assets_are_accessible_and_form_driven(tmp_path):
    client = _app(tmp_path).test_client()

    script = client.get("/static/web.js")
    styles = client.get("/static/web.css")

    assert script.status_code == 200
    assert b"form[data-loading-message]" in script.data
    assert b'form.setAttribute("aria-busy", "true")' in script.data
    assert b'window.addEventListener("pageshow", hideWaitState)' in script.data
    assert styles.status_code == 200
    assert b".running-puppet" in styles.data
    assert b"@media (prefers-reduced-motion: reduce)" in styles.data


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
        distance_m=12345.67,
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
    assert b'aria-label="Actual distance 12.35 kilometres"' in response.data
    assert b">12.35 km</span>" in response.data
    assert response.data.index(b"Execution score 92%") < response.data.index(b"12.35 km")
    styles = app.test_client().get("/static/web.css").data
    assert b".execution-score.good + .actual-distance" in styles


def _seed_calendar_for_export(app):
    today = date.today()
    entries = [
        (-2, "Elapsed", "scheduled"),
        (-1, "Completed", "completed"),
        (-1, "Missed", "missed"),
        (0, "Completed today", "completed"),
        (0, "Today run", "scheduled"),
        (1, "Skipped", "skipped"),
        (2, "Future run", "scheduled"),
    ]
    config = {
        "name": "Calendar block",
        "workouts": [
            {
                "date": (today + timedelta(days=offset)).isoformat(),
                "sport": "running",
                "name": name,
                "description": "Easy running; HR ≤140 bpm",
                "steps": [{"type": "interval", "duration": "30:00"}],
            }
            for offset, name, _status in entries
        ],
    }
    with AppState(app.config["DATA_DIR"]) as state:
        goal = state.save_goal(
            Goal(
                goal_type="complete_distance", description="Run ten kilometres", start_date=today, target_distance_km=10
            )
        )
        unrelated_config = {**config, "workouts": [{**config["workouts"][-1], "name": "Not active"}]}
        retired = state.save_plan(goal, unrelated_config, state.plans_dir / "old.yaml", "moderate", ())
        state.activate_plan(retired.id)
        active = state.save_plan(goal, config, state.plans_dir / "active.yaml", "moderate", ())
        state.activate_plan(active.id)
        state.save_plan(goal, unrelated_config, state.plans_dir / "draft.yaml", "moderate", ())
        for _offset, name, status in entries:
            state.connection.execute(
                "UPDATE plan_progress SET status = ? WHERE plan_id = ? AND workout_name = ?",
                (status, active.id, name),
            )
        state.connection.commit()
        return active, state.progress(active.id)


def test_calendar_download_only_exports_upcoming_active_runs_without_garmin_calls(tmp_path, monkeypatch):
    app = _app(tmp_path)
    active, progress_before = _seed_calendar_for_export(app)
    garmin = Mock(side_effect=AssertionError("Calendar export must not contact Garmin"))
    monkeypatch.setattr("garminworkouts.workflows.PlannerWorkflow.garmin_client", garmin)
    client = app.test_client()

    page = client.get("/calendar")
    response = client.get("/calendar.ics")
    repeated = client.get("/calendar.ics")

    assert b'href="/calendar.ics" download>Add to calendar (.ics)</a>' in page.data
    assert b"one-time import" in page.data
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/calendar; charset=utf-8"
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="running-plan-{active.start_date.isoformat()}.ics"'
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    content = response.get_data(as_text=True).replace("\r\n ", "")
    assert content.count("BEGIN:VEVENT\r\n") == 2
    assert "SUMMARY:Today run\r\n" in content
    assert "SUMMARY:Future run\r\n" in content
    assert "HR ≤140 bpm" in content
    for name in ("Elapsed", "Completed", "Missed", "Completed today", "Skipped", "Not active"):
        assert f"SUMMARY:{name}\r\n" not in content
    assert [line for line in response.data.splitlines() if line.startswith(b"UID:")] == [
        line for line in repeated.data.splitlines() if line.startswith(b"UID:")
    ]
    garmin.assert_not_called()
    with AppState(app.config["DATA_DIR"]) as state:
        assert state.progress(active.id) == progress_before


def test_calendar_download_is_unavailable_without_an_active_plan(tmp_path):
    client = _app(tmp_path).test_client()

    assert b'href="/calendar.ics"' not in client.get("/calendar").data
    assert client.get("/calendar.ics").status_code == 404


def test_calendar_download_is_unavailable_when_no_upcoming_runs_remain(tmp_path):
    app = _app(tmp_path)
    active, _progress = _seed_calendar_for_export(app)
    with AppState(app.config["DATA_DIR"]) as state:
        state.connection.execute("UPDATE plan_progress SET status = 'completed' WHERE plan_id = ?", (active.id,))
        state.connection.commit()
    client = app.test_client()

    assert b'href="/calendar.ics"' not in client.get("/calendar").data
    assert client.get("/calendar.ics").status_code == 404
