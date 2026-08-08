import sys
from datetime import date

import pytest

from garminworkouts.__main__ import main
from garminworkouts.app import InteractiveApp, _parse_clock, _parse_days, _parse_pace


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
