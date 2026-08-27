import sys
from datetime import date

import pytest

from garminworkouts.__main__ import main
from garminworkouts.app import (
    InteractiveApp,
    _format_heart_rate_targets,
    _parse_clock,
    _parse_days,
    _parse_heart_rate_target,
    _parse_pace,
)
from garminworkouts.llm import LLMConfig
from garminworkouts.state import AppState


@pytest.mark.parametrize("provider", ["anthropic", "openai-compatible"])
def test_cli_configures_provider_and_prompts_for_key_without_persisting_it(tmp_path, monkeypatch, provider):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RUNNING_PLANNER_LLM_API_KEY", raising=False)

    class ScriptedConsole:
        def __init__(self):
            self.messages = []
            self.enabled = True

        def confirm(self, prompt, default=False):
            return self.enabled

        def choose(self, prompt, options, default=1):
            assert "Claude (Anthropic)" in options
            return 2 if provider == "anthropic" else 1

        def ask(self, prompt, default=None):
            return default or "test-model"

        def secret(self, prompt):
            assert "hidden" in prompt
            return "test-only-secret"

        def write(self, message):
            self.messages.append(message)

    with AppState(tmp_path) as state:
        console = ScriptedConsole()
        app = InteractiveApp(state, console)
        app.configure_llm()
        config = LLMConfig.from_state(state)
        assert config.provider == provider
        assert config.enabled
        assert app._llm_session_key == (config, "test-only-secret")
        assert "test-only-secret" not in "\n".join(state.connection.iterdump())
        assert "test-only-secret" not in str(console.messages)
        console.enabled = False
        app.configure_llm()
        assert app._llm_session_key is None
        assert not LLMConfig.from_state(state).enabled


def test_interactive_input_parsers():
    assert _parse_clock("55:00") == 3300
    assert _parse_clock("1:05:00") == 3900
    assert _parse_pace("5:30") == 330
    assert _parse_days("Tue, Thu, Sun") == (1, 3, 6)


@pytest.mark.parametrize("value", ["fast", "1:99", "5"])
def test_invalid_pace_is_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        _parse_pace(value)


def test_help_never_prints_credential_defaults(monkeypatch, capsys):
    secret = "DO-NOT-PRINT-THIS-PASSWORD"
    monkeypatch.setenv("GARMIN_USERNAME", "runner@example.test")
    monkeypatch.setenv("GARMIN_PASSWORD", secret)
    monkeypatch.setattr(sys, "argv", ["garmin-workouts", "--help"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert secret not in capsys.readouterr().out


def test_calendar_changes_reports_only_future_removals():
    old = {
        "workouts": [
            {"date": "2030-01-01", "name": "Past", "description": "same", "steps": []},
            {"date": "2030-01-10", "name": "Keep", "description": "same", "steps": []},
            {"date": "2030-01-12", "name": "Remove", "description": "same", "steps": []},
        ]
    }
    new = {"workouts": [{"date": "2030-01-10", "name": "Keep", "description": "same", "steps": []}]}

    changes = InteractiveApp._calendar_changes(old, new, today=date(2030, 1, 5))

    assert changes == ["2030-01-12: remove Remove"]


def test_interactive_heart_rate_targets_support_caps_ranges_and_zones():
    assert _parse_heart_rate_target("heart_rate_max", "140") == {"heart_rate_max": 140}
    assert _parse_heart_rate_target("heart_rate", "120-145") == {"heart_rate": [120, 145]}
    assert _parse_heart_rate_target("heart_rate_zone", "3") == {"heart_rate_zone": 3}
    assert (
        _format_heart_rate_targets({"easy": {"heart_rate_max": 140}, "quality": {"heart_rate_zone": 4}})
        == "Easy running ≤140 bpm; Quality work intervals zone 4"
    )


def test_heart_rate_wizard_collects_phase_caps_and_resolves_pace_conflict():
    class ScriptedConsole:
        def __init__(self):
            self.answers = iter(["120", "140", "145", "165", "135"])
            self.choices = iter([1, 2])

        def confirm(self, prompt, default=False):
            return True

        def choose(self, prompt, options, default=1):
            return next(self.choices)

        def ask(self, prompt, default=None):
            return next(self.answers)

        def write(self, message=""):
            pass

    app = InteractiveApp(state=None, console=ScriptedConsole())

    targets, preference = app.heart_rate_wizard(has_quality_pace=True)

    assert targets == {
        "warmup": {"heart_rate_max": 120},
        "easy": {"heart_rate_max": 140},
        "long": {"heart_rate_max": 145},
        "quality": {"heart_rate_max": 165},
        "recovery": {"heart_rate_max": 135},
    }
    assert preference == "heart_rate"


def test_schedule_conflict_review_requires_explicit_cleanup_consent(monkeypatch):
    class ScriptedConsole:
        def __init__(self, answers):
            self.answers = iter(answers)
            self.prompts = []

        def confirm(self, prompt, default=False):
            self.prompts.append(prompt)
            return next(self.answers)

        def write(self, message=""):
            pass

    class FakeCleanup:
        def __init__(self, plan, connection, today=None):
            pass

        def preview(self):
            return {
                "calendar": [{"date": "2030-01-10", "name": "Old workout"}],
                "templates": [{"workout_id": 1, "name": "Old workout"}],
                "summary": {
                    "overlapping_calendar_entries": 1,
                    "unresolved_calendar_entries": 0,
                    "obsolete_template_candidates": 1,
                },
            }

    monkeypatch.setattr("garminworkouts.app.ScheduledConflictCleanup", FakeCleanup)
    console = ScriptedConsole([True, False])
    decision = InteractiveApp(None, console=console, today=date(2030, 1, 1))._review_schedule_conflicts(
        object(), object()
    )

    assert decision["proceed"] is True
    assert decision["cleanup"] is not None
    assert decision["delete_templates"] is False
    assert decision["conflict_count"] == 1
    assert "after the new plan is uploaded" in console.prompts[0]
    assert "workout library" in console.prompts[1]


def test_standalone_overlap_cleanup_can_be_declined_without_duplicate_override(monkeypatch):
    class ScriptedConsole:
        def __init__(self):
            self.confirm_count = 0

        def confirm(self, prompt, default=False):
            self.confirm_count += 1
            return False

        def write(self, message=""):
            pass

    class FakeCleanup:
        def __init__(self, plan, connection, today=None):
            pass

        def preview(self):
            return {
                "calendar": [{"date": "2030-01-10", "name": "Old workout"}],
                "templates": [],
                "summary": {
                    "overlapping_calendar_entries": 1,
                    "unresolved_calendar_entries": 0,
                    "obsolete_template_candidates": 0,
                },
            }

    monkeypatch.setattr("garminworkouts.app.ScheduledConflictCleanup", FakeCleanup)
    console = ScriptedConsole()
    decision = InteractiveApp(None, console=console, today=date(2030, 1, 1))._review_schedule_conflicts(
        object(), object(), replacement_pending=False
    )

    assert decision["proceed"] is False
    assert decision["conflict_count"] == 1
    assert console.confirm_count == 1
