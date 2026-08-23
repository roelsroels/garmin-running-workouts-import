import copy
import math
import re
from datetime import date

MUTABLE_WORKOUT_STATUSES = {"scheduled"}


def nearest_five_minutes(value):
    """Round a positive duration to the nearest five minutes, with halves rounded up."""
    return max(5, int(math.floor(float(value) / 5 + 0.5) * 5))


def normalized_easy_minutes(value, maximum):
    maximum = max(20, int(maximum))
    maximum = max(20, maximum // 5 * 5)
    return max(20, min(nearest_five_minutes(value), maximum))


def progress_statuses(progress):
    return {(str(item["workout_date"]), str(item["workout_name"])): str(item["status"]) for item in (progress or [])}


def immutable_workout_keys(progress):
    return {
        (str(item["workout_date"]), str(item["workout_name"]))
        for item in (progress or [])
        if str(item["status"]) != "scheduled"
    }


def workout_status(workout, statuses, today):
    workout_date = date.fromisoformat(str(workout["date"]))
    return statuses.get(
        (workout_date.isoformat(), str(workout["name"])),
        "missed" if workout_date < today else "scheduled",
    )


def calendar_changes(old_config, new_config, today=None, progress=None):
    today = today or date.today()
    statuses = progress_statuses(progress)
    old = {str(item["date"]): item for item in old_config.get("workouts", [])}
    new = {str(item["date"]): item for item in new_config.get("workouts", [])}
    changes = []

    for item in new.values():
        item_date = date.fromisoformat(str(item["date"]))
        if item_date < today:
            continue
        previous = old.get(str(item["date"]))
        if previous is None:
            changes.append(f"{item['date']}: add {item['name']}")
        elif workout_status(previous, statuses, today) in MUTABLE_WORKOUT_STATUSES and not workouts_equivalent(
            previous, item
        ):
            changes.append(f"{item['date']}: {previous['name']} → {item['name']}")

    for item in old.values():
        item_date = date.fromisoformat(str(item["date"]))
        if (
            item_date >= today
            and str(item["date"]) not in new
            and workout_status(item, statuses, today) in MUTABLE_WORKOUT_STATUSES
        ):
            changes.append(f"{item['date']}: remove {item['name']}")
    return changes


def workouts_equivalent(first, second):
    first = _normalized_comparison(first)
    second = _normalized_comparison(second)
    return first == second


def _normalized_comparison(workout):
    comparison = {
        "sport": workout.get("sport"),
        "steps": copy.deepcopy(workout.get("steps")),
        "description": workout.get("description"),
    }
    if not _is_simple_duration_workout(comparison["steps"]):
        return comparison

    for step in comparison["steps"]:
        minutes = _whole_minutes(step["duration"])
        if minutes is not None:
            step["duration"] = f"{nearest_five_minutes(minutes)}:00"
    if comparison["description"]:
        comparison["description"] = re.sub(
            r"\b(\d+)\s+min\b",
            lambda match: f"{nearest_five_minutes(int(match.group(1)))} min",
            comparison["description"],
        )
    return comparison


def _is_simple_duration_workout(steps):
    return bool(steps) and all(
        isinstance(step, dict)
        and "duration" in step
        and not any(key in step for key in ("repeat", "distance", "lap_button"))
        for step in steps
    )


def _whole_minutes(value):
    parts = str(value).split(":")
    if len(parts) == 2 and parts[1] == "00" and parts[0].isdigit():
        return int(parts[0])
    if len(parts) == 3 and parts[2] == "00" and parts[1].isdigit() and parts[0].isdigit():
        return int(parts[0]) * 60 + int(parts[1])
    return None
