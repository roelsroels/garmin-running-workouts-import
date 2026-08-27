"""Offline iCalendar snapshots of upcoming runs (RFC 5545)."""

from datetime import UTC, date, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5


def upcoming_runs(rows, today):
    return sorted(
        (row for row in rows if row["status"] == "scheduled" and date.fromisoformat(str(row["date"])) >= today),
        key=lambda row: (str(row["date"]), row["name"]),
    )


def build_calendar(plan, workouts, exported_at=None):
    """Render dated workouts as all-day events without blocking the whole day."""
    timestamp = (exported_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    namespace = uuid5(NAMESPACE_URL, f"garmin-running-workouts:{plan.id}:{plan.created_at}")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Garmin Running Workouts Import//Training Calendar//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape_text(plan.name)}",
    ]
    for workout in workouts:
        workout_date = date.fromisoformat(str(workout["date"]))
        # Stable for repeated exports of this plan; no account details in the UID.
        uid = uuid5(namespace, f"{workout_date.isoformat()}:{workout['name']}")
        description = f"Training plan: {plan.name}\n\n{workout.get('description') or ''}"
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{timestamp}",
                f"DTSTART;VALUE=DATE:{workout_date:%Y%m%d}",
                # For all-day events, DTEND is the exclusive following date.
                f"DTEND;VALUE=DATE:{workout_date + timedelta(days=1):%Y%m%d}",
                f"SUMMARY:{_escape_text(workout['name'])}",
                f"DESCRIPTION:{_escape_text(description)}",
                "STATUS:CONFIRMED",
                "TRANSP:TRANSPARENT",
                "CLASS:PRIVATE",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_line(line) for line in lines) + "\r\n"


def _escape_text(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold_line(line):
    """Fold at 75 UTF-8 octets, never splitting a multibyte character."""
    result = []
    current = ""
    size = 0
    for character in line:
        width = len(character.encode("utf-8"))
        if size + width > 75:
            result.append(current)
            current, size = " ", 1
        current += character
        size += width
    result.append(current)
    return "\r\n".join(result)
