from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from garminworkouts.calendar_export import build_calendar, upcoming_runs


def _plan(**overrides):
    return SimpleNamespace(**{"id": 1, "created_at": "2026-08-01T12:00:00+00:00", "name": "Running block", **overrides})


def _run(**overrides):
    return {"date": "2026-08-27", "name": "Easy40 HR", "description": "40 min easy; HR ≤138 bpm", **overrides}


def _unfold(content):
    return content.replace("\r\n ", "").split("\r\n")


def test_calendar_has_required_fields_and_all_day_non_busy_event():
    instant = datetime(2026, 8, 27, 12, tzinfo=timezone(timedelta(hours=2)))

    result = build_calendar(_plan(), [_run()], exported_at=instant)
    lines = _unfold(result)

    assert result.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert result.endswith("END:VEVENT\r\nEND:VCALENDAR\r\n")
    assert "DTSTAMP:20260827T100000Z" in lines
    assert "DTSTART;VALUE=DATE:20260827" in lines
    assert "DTEND;VALUE=DATE:20260828" in lines
    assert "SUMMARY:Easy40 HR" in lines
    assert "DESCRIPTION:Training plan: Running block\\n\\n40 min easy\\; HR ≤138 bpm" in lines
    assert "TRANSP:TRANSPARENT" in lines
    assert "CLASS:PRIVATE" in lines
    assert "TZID" not in result
    assert "BEGIN:VALARM" not in result
    assert "\n" not in result.replace("\r\n", "")


@pytest.mark.parametrize(
    ("day", "end"),
    [("2028-02-29", "20280301"), ("2026-12-31", "20270101"), ("2026-10-25", "20261026")],
)
def test_calendar_date_end_is_exclusive_across_leap_year_month_year_and_dst(day, end):
    lines = _unfold(build_calendar(_plan(), [_run(date=day)]))

    assert f"DTSTART;VALUE=DATE:{day.replace('-', '')}" in lines
    assert f"DTEND;VALUE=DATE:{end}" in lines


def test_calendar_escapes_text_and_prevents_component_injection():
    result = build_calendar(
        _plan(name="Plan, two; phases"),
        [_run(name="Easy, steady; path\\trail\r\nBEGIN:VEVENT", description="First\rSecond\nThird")],
    )
    lines = _unfold(result)

    assert lines.count("BEGIN:VEVENT") == 1
    assert "X-WR-CALNAME:Plan\\, two\\; phases" in lines
    assert "SUMMARY:Easy\\, steady\\; path\\\\trail\\nBEGIN:VEVENT" in lines
    assert "DESCRIPTION:Training plan: Plan\\, two\\; phases\\n\\nFirst\\nSecond\\nThird" in lines


def test_calendar_folds_unicode_lines_without_splitting_utf8_characters():
    name = "é🏃" * 70
    result = build_calendar(_plan(), [_run(name=name, description=name)])

    assert "\r\n " in result
    assert all(len(line.encode("utf-8")) <= 75 for line in result.split("\r\n"))
    assert f"SUMMARY:{name}" in _unfold(result)
    assert result.encode("utf-8").decode("utf-8") == result


def test_event_uids_are_stable_for_reexports_and_unique_between_events_and_plans():
    workouts = [_run(), _run(date="2026-08-28"), _run(name="Another run")]
    first = build_calendar(_plan(), workouts, exported_at=datetime(2026, 8, 27, tzinfo=UTC))
    later = build_calendar(_plan(), workouts, exported_at=datetime(2026, 8, 28, tzinfo=UTC))
    other = build_calendar(_plan(id=2), workouts)
    first_ids = [line for line in _unfold(first) if line.startswith("UID:")]

    assert len(set(first_ids)) == 3
    assert first_ids == [line for line in _unfold(later) if line.startswith("UID:")]
    assert not set(first_ids) & {line for line in _unfold(other) if line.startswith("UID:")}


def test_upcoming_runs_include_today_and_exclude_past_and_finished_rows():
    rows = [
        _run(date="2026-08-29", status="scheduled"),
        _run(date="2026-08-26", status="scheduled"),
        _run(date="2026-08-28", status="scheduled"),
        _run(status="scheduled"),
        *[_run(status=status) for status in ("completed", "missed", "skipped", "retired")],
    ]

    result = upcoming_runs(rows, date(2026, 8, 27))

    assert [row["date"] for row in result] == ["2026-08-27", "2026-08-28", "2026-08-29"]
    assert all(row["status"] == "scheduled" for row in result)
