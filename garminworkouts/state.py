import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from garminworkouts.models.heart_rate import HeartRateRange, validate_heart_rate_zone

SCHEMA_VERSION = 3

HEART_RATE_PHASES = {"warmup", "easy", "long", "quality", "recovery"}


def _now():
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Goal:
    goal_type: str
    description: str
    start_date: date
    plan_weeks: int = 4
    available_days: tuple[int, ...] = (1, 3, 6)
    long_run_day: int = 6
    target_date: date | None = None
    target_distance_km: float | None = None
    target_time_seconds: int | None = None
    target_pace_seconds_per_km: int | None = None
    target_duration_minutes: int | None = None
    runs_per_week: int = 3
    max_session_minutes: int = 90
    baseline_long_run_km: float | None = None
    heart_rate_targets: dict = field(default_factory=dict)
    quality_target_preference: str = "pace"
    constraints: str = ""
    id: int | None = field(default=None, compare=False)

    TYPES = {
        "complete_distance",
        "target_time",
        "sustain_pace",
        "endurance",
        "speed",
        "consistency",
    }

    def __post_init__(self):
        if self.goal_type not in self.TYPES:
            raise ValueError(f"Unsupported goal type '{self.goal_type}'")
        if not 1 <= self.plan_weeks <= 12:
            raise ValueError("Planning period must be between 1 and 12 weeks")
        if not 1 <= self.runs_per_week <= 7:
            raise ValueError("Runs per week must be between 1 and 7")
        days = tuple(sorted(set(self.available_days)))
        if not days or any(day < 0 or day > 6 for day in days):
            raise ValueError("Available days must use weekday numbers 0-6")
        if len(days) < self.runs_per_week:
            raise ValueError("Available days must contain at least the requested number of runs")
        if self.long_run_day not in days:
            raise ValueError("The long-run day must be one of the available running days")
        if self.max_session_minutes < 15:
            raise ValueError("Maximum session duration must be at least 15 minutes")
        if self.target_distance_km is not None and self.target_distance_km <= 0:
            raise ValueError("Target distance must be positive")
        if self.baseline_long_run_km is not None and self.baseline_long_run_km <= 0:
            raise ValueError("Baseline long-run distance must be positive")
        if self.goal_type in {"complete_distance", "target_time"} and not self.target_distance_km:
            raise ValueError(f"Goal type '{self.goal_type}' requires a target distance")
        if self.goal_type == "target_time" and not self.target_time_seconds:
            raise ValueError("A target-time goal requires a target time")
        if self.goal_type == "sustain_pace" and not self.target_pace_seconds_per_km:
            raise ValueError("A sustain-pace goal requires a target pace")
        if self.goal_type == "sustain_pace" and not self.target_duration_minutes:
            raise ValueError("A sustain-pace goal requires a target duration")
        if self.target_duration_minutes is not None and self.target_duration_minutes <= 0:
            raise ValueError("Target duration must be positive")
        if self.target_date is not None and self.target_date < self.start_date:
            raise ValueError("Target date cannot be before the plan start date")
        heart_rate_targets = _validated_heart_rate_targets(self.heart_rate_targets)
        if self.quality_target_preference not in {"pace", "heart_rate"}:
            raise ValueError("Quality target preference must be 'pace' or 'heart_rate'")
        if self.quality_target_preference == "heart_rate" and "quality" not in heart_rate_targets:
            raise ValueError("A heart-rate quality preference requires a quality heart-rate target")
        object.__setattr__(self, "available_days", days)
        object.__setattr__(self, "heart_rate_targets", heart_rate_targets)

    def to_dict(self):
        result = asdict(self)
        result["start_date"] = self.start_date.isoformat()
        result["target_date"] = self.target_date.isoformat() if self.target_date else None
        result["available_days"] = list(self.available_days)
        return result

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value["start_date"] = date.fromisoformat(value["start_date"])
        if value.get("target_date"):
            value["target_date"] = date.fromisoformat(value["target_date"])
        value["available_days"] = tuple(value["available_days"])
        return cls(**value)

    @property
    def target_pace(self):
        if self.target_pace_seconds_per_km:
            return self.target_pace_seconds_per_km
        if self.target_time_seconds and self.target_distance_km:
            return round(self.target_time_seconds / self.target_distance_km)
        return None


def _validated_heart_rate_targets(value):
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("Heart-rate targets must be a mapping by workout phase")
    result = {}
    for phase, target in value.items():
        if phase not in HEART_RATE_PHASES:
            raise ValueError(f"Unsupported heart-rate phase '{phase}'")
        if not isinstance(target, dict):
            raise ValueError(f"Heart-rate target for '{phase}' must be a mapping")
        keys = [key for key in ("heart_rate_max", "heart_rate", "heart_rate_zone") if key in target]
        if len(keys) != 1:
            raise ValueError(f"Heart-rate target for '{phase}' must define exactly one target type")
        key = keys[0]
        if key == "heart_rate_max":
            heart_rate = HeartRateRange.from_maximum(target[key])
            result[phase] = {key: heart_rate.upper}
        elif key == "heart_rate":
            heart_rate = HeartRateRange.from_config(target[key])
            result[phase] = {key: list(heart_rate.to_bpm_bounds())}
        else:
            result[phase] = {key: validate_heart_rate_zone(target[key])}
    return result


@dataclass(frozen=True)
class PlanRecord:
    id: int
    goal_id: int
    name: str
    start_date: date
    end_date: date
    path: Path
    config: dict
    status: str
    confidence: str
    rationale: tuple[str, ...]
    created_at: str
    applied_at: str | None = None
    supersedes_plan_id: int | None = None


class AppState:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._make_private(self.data_dir, 0o700)
        self.plans_dir = self.data_dir / "plans"
        self.activities_dir = self.data_dir / "activities"
        self.tokens_dir = self.data_dir / "tokens"
        for directory in (self.plans_dir, self.activities_dir, self.tokens_dir):
            directory.mkdir(parents=True, exist_ok=True)
            self._make_private(directory, 0o700)
        self.database_path = self.data_dir / "state.sqlite3"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()
        self._make_private(self.database_path, 0o600)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @staticmethod
    def _make_private(path, mode):
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    def _create_schema(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                path TEXT NOT NULL,
                config_json TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence TEXT NOT NULL,
                rationale_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                applied_at TEXT,
                supersedes_plan_id INTEGER,
                FOREIGN KEY(goal_id) REFERENCES goals(id)
            );
            CREATE TABLE IF NOT EXISTS plan_progress (
                plan_id INTEGER NOT NULL,
                workout_date TEXT NOT NULL,
                workout_name TEXT NOT NULL,
                status TEXT NOT NULL,
                activity_id TEXT,
                actual_distance_m REAL,
                execution_score REAL,
                execution_score_checked_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(plan_id, workout_date, workout_name),
                FOREIGN KEY(plan_id) REFERENCES plans(id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        progress_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(plan_progress)").fetchall()
        }
        if "execution_score" not in progress_columns:
            self.connection.execute("ALTER TABLE plan_progress ADD COLUMN execution_score REAL")
        if "execution_score_checked_at" not in progress_columns:
            self.connection.execute("ALTER TABLE plan_progress ADD COLUMN execution_score_checked_at TEXT")
        if "actual_distance_m" not in progress_columns:
            self.connection.execute("ALTER TABLE plan_progress ADD COLUMN actual_distance_m REAL")
        self.set_setting("schema_version", str(SCHEMA_VERSION))
        self.connection.commit()

    def set_setting(self, key, value):
        self.connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.connection.commit()

    def get_setting(self, key, default=None):
        row = self.connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def save_goal(self, goal, activate=True):
        goal_data = goal.to_dict()
        goal_data.pop("id", None)
        now = _now()
        with self.connection:
            if activate:
                self.connection.execute(
                    "UPDATE goals SET status = 'retired', updated_at = ? WHERE status = 'active'", (now,)
                )
            cursor = self.connection.execute(
                "INSERT INTO goals(status, data_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("active" if activate else "draft", json.dumps(goal_data), now, now),
            )
        return Goal.from_dict({**goal_data, "id": cursor.lastrowid})

    def active_goal(self):
        row = self.connection.execute(
            "SELECT id, data_json FROM goals WHERE status = 'active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return Goal.from_dict({**json.loads(row["data_json"]), "id": row["id"]})

    def save_plan(self, goal, config, path, confidence, rationale, status="proposed", supersedes_plan_id=None):
        if goal.id is None:
            raise ValueError("A plan can only be saved for a persisted goal")
        workouts = config.get("workouts") or []
        if not workouts:
            raise ValueError("Cannot save an empty plan")
        dates = sorted(date.fromisoformat(str(workout["date"])) for workout in workouts)
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO plans(
                    goal_id, name, start_date, end_date, path, config_json, status,
                    confidence, rationale_json, created_at, supersedes_plan_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal.id,
                    config.get("name", "Training plan"),
                    dates[0].isoformat(),
                    dates[-1].isoformat(),
                    str(Path(path).resolve()),
                    json.dumps(config),
                    status,
                    confidence,
                    json.dumps(list(rationale)),
                    now,
                    supersedes_plan_id,
                ),
            )
            plan_id = cursor.lastrowid
            for workout in workouts:
                self.connection.execute(
                    """
                    INSERT INTO plan_progress(plan_id, workout_date, workout_name, status, updated_at)
                    VALUES (?, ?, ?, 'scheduled', ?)
                    """,
                    (plan_id, str(workout["date"]), workout["name"], now),
                )
        return self.plan(plan_id)

    def plan(self, plan_id):
        row = self.connection.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return self._plan_from_row(row) if row else None

    def active_plan(self):
        row = self.connection.execute("SELECT * FROM plans WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone()
        return self._plan_from_row(row) if row else None

    @staticmethod
    def _plan_from_row(row):
        return PlanRecord(
            id=row["id"],
            goal_id=row["goal_id"],
            name=row["name"],
            start_date=date.fromisoformat(row["start_date"]),
            end_date=date.fromisoformat(row["end_date"]),
            path=Path(row["path"]),
            config=json.loads(row["config_json"]),
            status=row["status"],
            confidence=row["confidence"],
            rationale=tuple(json.loads(row["rationale_json"])),
            created_at=row["created_at"],
            applied_at=row["applied_at"],
            supersedes_plan_id=row["supersedes_plan_id"],
        )

    def activate_plan(self, plan_id):
        now = _now()
        with self.connection:
            self.connection.execute(
                "UPDATE plans SET status = 'retired' WHERE status = 'active' AND id != ?", (plan_id,)
            )
            self.connection.execute(
                "UPDATE plans SET status = 'active', applied_at = ? WHERE id = ?",
                (now, plan_id),
            )

    def mark_plan_failed(self, plan_id, reason):
        with self.connection:
            self.connection.execute("UPDATE plans SET status = 'apply-failed' WHERE id = ?", (plan_id,))
        self.record_event("plan-apply-failed", {"plan_id": plan_id, "reason": str(reason)})

    def refresh_progress(self, plan_id, activities, today=None):
        today = today or date.today()
        activities_by_date = {}
        for activity in sorted(activities, key=lambda item: item.started_at):
            activities_by_date.setdefault(activity.date.isoformat(), activity)
        rows = self.connection.execute(
            """
            SELECT workout_date, workout_name, activity_id, actual_distance_m,
                   execution_score, execution_score_checked_at
            FROM plan_progress WHERE plan_id = ? ORDER BY workout_date
            """,
            (plan_id,),
        ).fetchall()
        now = _now()
        with self.connection:
            for row in rows:
                activity = activities_by_date.get(row["workout_date"])
                workout_date = date.fromisoformat(row["workout_date"])
                if activity:
                    status = "completed"
                    activity_id = activity.activity_id
                    same_activity = row["activity_id"] == activity_id
                    actual_distance_m = (
                        activity.distance_m
                        if activity.distance_m is not None
                        else row["actual_distance_m"]
                        if same_activity
                        else None
                    )
                    execution_score = (
                        activity.execution_score
                        if activity.execution_score is not None
                        else row["execution_score"]
                        if same_activity
                        else None
                    )
                    execution_score_checked_at = (
                        now
                        if activity.execution_score_checked
                        else row["execution_score_checked_at"]
                        if same_activity
                        else None
                    )
                elif workout_date < today:
                    status = "missed"
                    activity_id = None
                    actual_distance_m = None
                    execution_score = None
                    execution_score_checked_at = None
                else:
                    status = "scheduled"
                    activity_id = None
                    actual_distance_m = None
                    execution_score = None
                    execution_score_checked_at = None
                self.connection.execute(
                    """
                    UPDATE plan_progress
                    SET status = ?, activity_id = ?, actual_distance_m = ?, execution_score = ?,
                        execution_score_checked_at = ?, updated_at = ?
                    WHERE plan_id = ? AND workout_date = ? AND workout_name = ?
                    """,
                    (
                        status,
                        activity_id,
                        actual_distance_m,
                        execution_score,
                        execution_score_checked_at,
                        now,
                        plan_id,
                        row["workout_date"],
                        row["workout_name"],
                    ),
                )
        return self.progress(plan_id)

    def progress(self, plan_id):
        rows = self.connection.execute(
            """
            SELECT workout_date, workout_name, status, activity_id, actual_distance_m,
                   execution_score, execution_score_checked_at
            FROM plan_progress WHERE plan_id = ? ORDER BY workout_date, workout_name
            """,
            (plan_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def progress_summary(self, plan_id):
        rows = self.progress(plan_id)
        return self._summarize_progress(rows)

    def block_progress(self, plan_id):
        """Return current progress plus the elapsed portion of superseded plans."""
        current = self.plan(plan_id)
        if current is None:
            return []
        rows = self.progress(plan_id)
        combined = {(row["workout_date"], row["workout_name"]): row for row in rows}
        cutoff = min((date.fromisoformat(row["workout_date"]) for row in rows), default=current.start_date)
        parent_id = current.supersedes_plan_id
        while parent_id:
            parent = self.plan(parent_id)
            if parent is None:
                break
            inherited_dates = []
            for row in self.progress(parent_id):
                workout_date = date.fromisoformat(row["workout_date"])
                key = (row["workout_date"], row["workout_name"])
                if workout_date < cutoff and row["status"] in {"completed", "missed"} and key not in combined:
                    combined[key] = row
                    inherited_dates.append(workout_date)
            if inherited_dates:
                cutoff = min(cutoff, min(inherited_dates))
            parent_id = parent.supersedes_plan_id
        return sorted(combined.values(), key=lambda row: (row["workout_date"], row["workout_name"]))

    def block_progress_summary(self, plan_id):
        return self._summarize_progress(self.block_progress(plan_id))

    @staticmethod
    def _summarize_progress(rows):
        counts = {"total": len(rows), "completed": 0, "missed": 0, "scheduled": 0}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        counts["remaining"] = counts["scheduled"]
        return counts

    def record_event(self, event_type, payload):
        self.connection.execute(
            "INSERT INTO events(event_type, payload_json, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload), _now()),
        )
        self.connection.commit()
