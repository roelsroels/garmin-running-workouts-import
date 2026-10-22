"""Safe, idempotent plan publishing through Garmin's official Training API."""

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

from garminworkouts.garmin.starterclient import GarminStarterNotFoundError
from garminworkouts.garmin.training_payload import official_running_workout_payload

REGISTRY_FILENAME = "remote-objects.json"


class OfficialObjectRegistry:
    """Private record of remote objects created by this application.

    The official Training API deliberately has no workout-library listing
    endpoint. Keeping remote IDs is therefore required for safe updates and
    lets cleanup prove ownership before deleting anything.
    """

    def __init__(self, directory, user_id):
        self.directory = Path(directory).expanduser().resolve()
        self.path = self.directory / REGISTRY_FILENAME
        self.user_id = str(user_id or "").strip()
        if not self.user_id:
            raise ValueError("An official Garmin user ID is required")
        self.data = self._load()

    def workout(self, name):
        return self.data["workouts"].get(str(name))

    def schedules(self):
        return list(self.data["schedules"].values())

    def schedule(self, workout_date, name):
        return self.data["schedules"].get(self._schedule_key(workout_date, name))

    def owns_workout(self, workout_id):
        workout_id = str(workout_id)
        return any(str(item.get("workout_id")) == workout_id for item in self.data["workouts"].values())

    def record_workout(self, name, workout_id, payload_hash):
        self.data["workouts"][str(name)] = {
            "name": str(name),
            "workout_id": int(workout_id),
            "payload_hash": str(payload_hash),
            "updated_at": _now(),
        }
        self.save()

    def forget_workout(self, name=None, workout_id=None):
        names = [
            key
            for key, item in self.data["workouts"].items()
            if (name is not None and key == str(name))
            or (workout_id is not None and str(item.get("workout_id")) == str(workout_id))
        ]
        for key in names:
            self.data["workouts"].pop(key, None)
        if names:
            self.save()

    def record_schedule(self, workout_date, name, schedule_id, workout_id, plan_id):
        key = self._schedule_key(workout_date, name)
        self.data["schedules"][key] = {
            "date": str(workout_date)[:10],
            "name": str(name),
            "schedule_id": int(schedule_id),
            "workout_id": int(workout_id),
            "plan_id": str(plan_id),
            "updated_at": _now(),
        }
        self.save()

    def forget_schedule(self, schedule_id=None, workout_date=None, name=None):
        exact_key = self._schedule_key(workout_date, name) if workout_date is not None and name is not None else None
        keys = [
            key
            for key, item in self.data["schedules"].items()
            if key == exact_key or (schedule_id is not None and str(item.get("schedule_id")) == str(schedule_id))
        ]
        for key in keys:
            self.data["schedules"].pop(key, None)
        if keys:
            self.save()

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        try:
            os.chmod(self.directory, 0o700)
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)

    def _load(self):
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            value = {}
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("The official Garmin remote-object registry is invalid") from exc
        if value and value.get("user_id") != self.user_id:
            raise ValueError("The official Garmin remote-object registry belongs to another connected user")
        workouts = value.get("workouts", {}) if isinstance(value, dict) else {}
        schedules = value.get("schedules", {}) if isinstance(value, dict) else {}
        if not isinstance(workouts, dict) or not isinstance(schedules, dict):
            raise ValueError("The official Garmin remote-object registry is invalid")
        return {
            "version": 1,
            "user_id": self.user_id,
            "workouts": workouts,
            "schedules": schedules,
        }

    @staticmethod
    def _schedule_key(workout_date, name):
        return f"{str(workout_date)[:10]}|{name}"


class OfficialTrainingPublisher:
    """Publish and retire app-owned workouts without library-wide access."""

    def __init__(self, plan, connection, registry, plan_id, today=None):
        self.plan = plan
        self.connection = connection
        self.registry = registry
        self.plan_id = str(plan_id)
        self.today = today or date.today()

    def preview_conflicts(self):
        entries = [entry for entry in self.plan.entries if entry["date"] >= self.today]
        target_dates = {entry["date"].isoformat() for entry in entries}
        protected_keys = {(entry["date"].isoformat(), entry["workout"].get_workout_name()) for entry in entries}
        protected_seen = set()
        calendar = []
        workout_cache = {}
        for item in self._remote_schedules(entries):
            if item["date"] not in target_dates:
                continue
            workout_id = item["workout_id"]
            name = self._workout_name(workout_id, workout_cache)
            key = (item["date"], name)
            recorded = self.registry.workout(name)
            app_owned_exact = recorded and str(recorded.get("workout_id")) == str(workout_id)
            if key in protected_keys and app_owned_exact and key not in protected_seen:
                protected_seen.add(key)
                continue
            schedule_id = item.get("schedule_id")
            calendar.append(
                {
                    "date": item["date"],
                    "name": name,
                    "workout_id": workout_id,
                    "scheduled_workout_id": schedule_id,
                    "action": "unschedule" if schedule_id is not None else "unresolved-schedule-id",
                    "template_can_be_deleted": self.registry.owns_workout(workout_id)
                    and name not in {key[1] for key in protected_keys},
                }
            )
        templates = {
            str(item["workout_id"]): {"workout_id": item["workout_id"], "name": item["name"]}
            for item in calendar
            if item["template_can_be_deleted"]
        }
        calendar.sort(key=lambda item: (item["date"], item["name"], str(item["scheduled_workout_id"])))
        return {
            "calendar": calendar,
            "templates": sorted(templates.values(), key=lambda item: item["name"]),
            "summary": {
                "overlapping_calendar_entries": len(calendar),
                "unresolved_calendar_entries": sum(item["action"] == "unresolved-schedule-id" for item in calendar),
                "obsolete_template_candidates": len(templates),
            },
            "warnings": [
                "Only scheduled workouts on replacement dates are included.",
                "Workout deletion is limited to IDs previously created and recorded by this application.",
                "Completed activity/FIT records are never deleted.",
            ],
        }

    def apply(self, schedule=True):
        actions = []
        workout_ids = {}
        for workout in self.plan.unique_workouts():
            name = workout.get_workout_name()
            payload = official_running_workout_payload(workout)
            payload_hash = _payload_hash(payload)
            recorded = self.registry.workout(name)
            workout_id = None
            if recorded:
                workout_id = recorded["workout_id"]
                try:
                    remote = self.connection.get_workout(workout_id)
                    update_payload = dict(payload)
                    update_payload["workoutId"] = int(workout_id)
                    if isinstance(remote, dict) and remote.get("ownerId") is not None:
                        update_payload["ownerId"] = remote["ownerId"]
                    self.connection.update_workout(workout_id, update_payload)
                    actions.append({"action": "updated", "name": name, "workout_id": workout_id})
                except GarminStarterNotFoundError:
                    self.registry.forget_workout(name=name)
                    workout_id = None
            if workout_id is None:
                response = self.connection.save_workout(payload)
                workout_id = _find_id(response, "workoutId")
                if workout_id is None:
                    raise RuntimeError(
                        f"Garmin created workout '{name}' without returning an ID; publishing stopped safely"
                    )
                actions.append({"action": "created", "name": name, "workout_id": workout_id})
            self.registry.record_workout(name, workout_id, payload_hash)
            workout_ids[name] = int(workout_id)

        if not schedule:
            return actions
        existing = {(str(item["workout_id"]), item["date"]): item for item in self._remote_schedules(self.plan.entries)}
        for entry in self.plan.entries:
            name = entry["workout"].get_workout_name()
            workout_id = workout_ids[name]
            date_text = entry["date"].isoformat()
            remote = existing.get((str(workout_id), date_text))
            if remote:
                schedule_id = remote.get("schedule_id")
                actions.append(
                    {
                        "action": "schedule-skipped",
                        "name": name,
                        "workout_id": workout_id,
                        "date": date_text,
                    }
                )
            else:
                response = self.connection.schedule_workout(workout_id, date_text)
                schedule_id = _find_id(response, "scheduleId")
                if schedule_id is None:
                    schedule_id = self._resolve_schedule_id(workout_id, date_text)
                if schedule_id is None:
                    raise RuntimeError(f"Garmin scheduled workout '{name}' without returning a resolvable schedule ID")
                actions.append({"action": "scheduled", "name": name, "workout_id": workout_id, "date": date_text})
            if schedule_id is not None:
                self.registry.record_schedule(date_text, name, schedule_id, workout_id, self.plan_id)
        return actions

    def apply_conflicts(self, preview, delete_templates=False):
        if any(item["action"] == "unresolved-schedule-id" for item in preview["calendar"]):
            raise RuntimeError("Cannot replace schedules while a Garmin schedule ID is unresolved")
        actions = []
        for item in preview["calendar"]:
            self.connection.unschedule_workout(item["scheduled_workout_id"])
            self.registry.forget_schedule(schedule_id=item["scheduled_workout_id"])
            actions.append(
                {
                    "action": "unscheduled-conflict",
                    **{key: item[key] for key in ("date", "name", "workout_id", "scheduled_workout_id")},
                }
            )
        if delete_templates:
            for item in preview["templates"]:
                if not self.registry.owns_workout(item["workout_id"]):
                    continue
                self.connection.delete_workout(item["workout_id"])
                self.registry.forget_workout(workout_id=item["workout_id"])
                actions.append({"action": "deleted-conflicting-workout-template", **item})
        return actions

    def retire(self, old_plan, old_plan_ids, protected_plan=None, delete_finished_templates=False):
        old_plan_ids = {str(value) for value in old_plan_ids}
        protected_names = {
            entry["workout"].get_workout_name() for entry in (protected_plan.entries if protected_plan else [])
        }
        actions = []
        for item in list(self.registry.schedules()):
            if str(item.get("plan_id")) not in old_plan_ids or item["date"] < self.today.isoformat():
                continue
            if (item["date"], item["name"]) in {
                (entry["date"].isoformat(), entry["workout"].get_workout_name())
                for entry in (protected_plan.entries if protected_plan else [])
            }:
                continue
            try:
                self.connection.unschedule_workout(item["schedule_id"])
            except GarminStarterNotFoundError:
                pass
            self.registry.forget_schedule(schedule_id=item["schedule_id"])
            actions.append(
                {
                    "action": "unscheduled",
                    "date": item["date"],
                    "name": item["name"],
                    "workout_id": item["workout_id"],
                    "scheduled_workout_id": item["schedule_id"],
                }
            )

        if delete_finished_templates:
            old_names = {entry["workout"].get_workout_name() for entry in old_plan.entries}
            scheduled_ids = {
                str(item["workout_id"]) for item in self.registry.schedules() if item["date"] >= self.today.isoformat()
            }
            for name in sorted(old_names - protected_names):
                recorded = self.registry.workout(name)
                if not recorded or str(recorded["workout_id"]) in scheduled_ids:
                    continue
                try:
                    self.connection.delete_workout(recorded["workout_id"])
                except GarminStarterNotFoundError:
                    pass
                self.registry.forget_workout(name=name)
                actions.append(
                    {"action": "deleted-workout-template", "name": name, "workout_id": recorded["workout_id"]}
                )
        return actions

    def preview_retirement_templates(self, old_plan, protected_plan=None):
        protected_names = {
            entry["workout"].get_workout_name() for entry in (protected_plan.entries if protected_plan else [])
        }
        workouts = []
        for name in sorted({entry["workout"].get_workout_name() for entry in old_plan.entries}):
            recorded = self.registry.workout(name)
            if not recorded:
                workouts.append({"name": name, "workout_id": None, "action": "missing"})
            elif name in protected_names:
                workouts.append({"name": name, "workout_id": recorded["workout_id"], "action": "retain-protected"})
            else:
                workouts.append({"name": name, "workout_id": recorded["workout_id"], "action": "delete"})
        return {
            "plan": old_plan.name,
            "today": self.today.isoformat(),
            "mode": "preview-only; no Garmin Connect changes were made",
            "summary": {
                "future_calendar_entries_to_unschedule": 0,
                "past_calendar_entries_retained": 0,
                "immutable_calendar_entries_retained": 0,
                "workout_templates_to_delete": sum(item["action"] == "delete" for item in workouts),
                "protected_workout_templates": sum(item["action"] == "retain-protected" for item in workouts),
                "immutable_workout_templates": 0,
                "missing_workout_templates": sum(item["action"] == "missing" for item in workouts),
            },
            "calendar": [],
            "workouts": workouts,
            "warnings": [
                "Only workout IDs previously created and recorded by this application can be deleted.",
                "Completed activity/FIT records are outside this operation and will not be deleted.",
            ],
        }

    def apply_retirement_templates(self, preview):
        actions = []
        for item in preview["workouts"]:
            if item["action"] != "delete" or not self.registry.owns_workout(item["workout_id"]):
                continue
            try:
                self.connection.delete_workout(item["workout_id"])
            except GarminStarterNotFoundError:
                pass
            self.registry.forget_workout(workout_id=item["workout_id"])
            actions.append(
                {"action": "deleted-workout-template", "name": item["name"], "workout_id": item["workout_id"]}
            )
        return actions

    def _remote_schedules(self, entries):
        entries = list(entries)
        if not entries:
            return []
        start = min(entry["date"] for entry in entries).isoformat()
        end = max(entry["date"] for entry in entries).isoformat()
        return _extract_schedules(self.connection.list_scheduled_workouts(start, end))

    def _resolve_schedule_id(self, workout_id, workout_date):
        items = _extract_schedules(self.connection.list_scheduled_workouts(workout_date, workout_date))
        matches = [
            item["schedule_id"]
            for item in items
            if str(item["workout_id"]) == str(workout_id) and item["date"] == workout_date and item.get("schedule_id")
        ]
        return matches[0] if len(matches) == 1 else None

    def _workout_name(self, workout_id, cache):
        if str(workout_id) not in cache:
            try:
                workout = self.connection.get_workout(workout_id)
            except GarminStarterNotFoundError:
                workout = {}
            cache[str(workout_id)] = str((workout or {}).get("workoutName") or f"Garmin workout {workout_id}")
        return cache[str(workout_id)]


def _extract_schedules(value, inherited_date=None):
    items = []
    if isinstance(value, list):
        for item in value:
            items.extend(_extract_schedules(item, inherited_date))
        return _deduplicate_schedules(items)
    if not isinstance(value, dict):
        return items
    workout_date = next(
        (str(value[key])[:10] for key in ("date", "calendarDate", "scheduledDate") if value.get(key)),
        inherited_date,
    )
    workout_id = value.get("workoutId")
    if workout_id is None and isinstance(value.get("workout"), dict):
        workout_id = value["workout"].get("workoutId")
    schedule_id = next(
        (
            value.get(key)
            for key in ("scheduleId", "scheduledWorkoutId", "workoutScheduleId")
            if value.get(key) is not None
        ),
        None,
    )
    if workout_id is not None and workout_date:
        items.append({"date": workout_date, "workout_id": workout_id, "schedule_id": schedule_id})
    for nested in value.values():
        if isinstance(nested, (dict, list)):
            items.extend(_extract_schedules(nested, workout_date))
    return _deduplicate_schedules(items)


def _deduplicate_schedules(items):
    result = []
    seen = set()
    for item in items:
        key = (str(item.get("schedule_id")), str(item.get("workout_id")), item.get("date"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _find_id(value, key):
    if isinstance(value, dict):
        if value.get(key) is not None:
            return int(value[key])
        for nested in value.values():
            found = _find_id(nested, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_id(nested, key)
            if found is not None:
                return found
    return None


def _payload_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _now():
    return datetime.now(UTC).replace(microsecond=0).isoformat()
