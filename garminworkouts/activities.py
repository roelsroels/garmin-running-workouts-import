import hashlib
import io
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path


def _first_value(mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _parse_activity_datetime(activity):
    value = _first_value(activity, "startTimeLocal", "startTimeGMT")
    if value:
        text = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text)

    timestamp = activity.get("beginTimestamp")
    if timestamp is not None:
        return datetime.fromtimestamp(float(timestamp) / 1000, tz=UTC)

    raise ValueError(f"Activity {activity.get('activityId', '<unknown>')} has no usable start time")


def _number(value):
    return float(value) if value is not None else None


@dataclass(frozen=True)
class ActivitySummary:
    activity_id: str
    name: str
    started_at: datetime
    activity_type: str
    distance_m: float | None = None
    duration_s: float | None = None
    moving_duration_s: float | None = None
    average_hr: float | None = None
    max_hr: float | None = None
    average_speed_mps: float | None = None
    elevation_gain_m: float | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_garmin(cls, activity):
        activity_id = activity.get("activityId")
        if activity_id is None:
            raise ValueError("Garmin activity has no activityId")

        activity_type = activity.get("activityType") or activity.get("activityTypeDTO") or {}
        return cls(
            activity_id=str(activity_id),
            name=str(activity.get("activityName") or "Untitled activity"),
            started_at=_parse_activity_datetime(activity),
            activity_type=str(activity_type.get("typeKey") or activity_type.get("typeName") or "unknown"),
            distance_m=_number(activity.get("distance")),
            duration_s=_number(activity.get("duration")),
            moving_duration_s=_number(activity.get("movingDuration")),
            average_hr=_number(_first_value(activity, "averageHR", "avgHR")),
            max_hr=_number(activity.get("maxHR")),
            average_speed_mps=_number(activity.get("averageSpeed")),
            elevation_gain_m=_number(activity.get("elevationGain")),
            raw=activity,
        )

    @property
    def date(self):
        return self.started_at.date()

    @property
    def average_pace_seconds_per_km(self):
        if self.average_speed_mps and self.average_speed_mps > 0:
            return 1000 / self.average_speed_mps
        if self.duration_s and self.distance_m and self.distance_m > 0:
            return self.duration_s * 1000 / self.distance_m
        return None

    def to_dict(self):
        return {
            "activity_id": self.activity_id,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "activity_type": self.activity_type,
            "distance_m": self.distance_m,
            "duration_s": self.duration_s,
            "moving_duration_s": self.moving_duration_s,
            "average_hr": self.average_hr,
            "max_hr": self.max_hr,
            "average_speed_mps": self.average_speed_mps,
            "average_pace_seconds_per_km": self.average_pace_seconds_per_km,
            "elevation_gain_m": self.elevation_gain_m,
        }


@dataclass(frozen=True)
class AssessmentSelection:
    activities: tuple[ActivitySummary, ...]
    candidate_count: int
    rationale: tuple[str, ...]
    requested_count: int | None = None

    @property
    def recommended_count(self):
        return len(self.activities)

    @property
    def coverage_start(self):
        return min((activity.date for activity in self.activities), default=None)

    @property
    def coverage_end(self):
        return max((activity.date for activity in self.activities), default=None)

    def to_dict(self):
        return {
            "candidate_count": self.candidate_count,
            "recommended_count": self.recommended_count,
            "requested_count": self.requested_count,
            "coverage_start": self.coverage_start.isoformat() if self.coverage_start else None,
            "coverage_end": self.coverage_end.isoformat() if self.coverage_end else None,
            "rationale": list(self.rationale),
        }


class AssessmentSelector:
    def __init__(self, window_days=28, minimum_runs=6, maximum_runs=16):
        if window_days < 1:
            raise ValueError("window_days must be at least 1")
        if minimum_runs < 1:
            raise ValueError("minimum_runs must be at least 1")
        if maximum_runs < minimum_runs:
            raise ValueError("maximum_runs must be greater than or equal to minimum_runs")
        self.window_days = window_days
        self.minimum_runs = minimum_runs
        self.maximum_runs = maximum_runs

    def select(self, activities, last=None):
        ordered = sorted(activities, key=lambda activity: activity.started_at, reverse=True)
        if not ordered:
            return AssessmentSelection((), 0, ("No running activities were found in the requested period.",), last)

        if last is not None:
            if last < 1:
                raise ValueError("last must be at least 1")
            selected = ordered[:last]
            rationale = (f"Selected the explicitly requested {len(selected)} most recent running activities.",)
            return AssessmentSelection(tuple(selected), len(ordered), rationale, last)

        latest_date = ordered[0].date
        threshold = latest_date - timedelta(days=self.window_days - 1)
        window = [activity for activity in ordered if activity.date >= threshold]
        selected = window[: self.maximum_runs]
        rationale = [
            f"Selected recent runs from a {self.window_days}-day assessment window ending {latest_date.isoformat()}.",
        ]

        if len(window) > self.maximum_runs:
            rationale.append(
                f"Limited the set to the {self.maximum_runs} most recent runs to keep FIT analysis focused."
            )

        if len(selected) < self.minimum_runs:
            selected_ids = {activity.activity_id for activity in selected}
            older = [activity for activity in ordered if activity.activity_id not in selected_ids]
            needed = self.minimum_runs - len(selected)
            selected.extend(older[:needed])
            if older:
                rationale.append(
                    f"Included older runs to reach a minimum useful sample of {min(self.minimum_runs, len(ordered))}."
                )

        selected.sort(key=lambda activity: activity.started_at, reverse=True)
        if len(selected) < self.minimum_runs:
            rationale.append(f"Only {len(selected)} runs are available; conclusions should be treated as preliminary.")
        else:
            rationale.append(
                "The set is intended to cover easy, quality, and long-run responses across a full training block."
            )
        return AssessmentSelection(tuple(selected), len(ordered), tuple(rationale))


class ActivityArchive:
    MANIFEST_VERSION = 1

    def __init__(self, destination):
        self.destination = Path(destination)

    def prepare(self, selection, connection, overwrite=False):
        self.destination.mkdir(parents=True, exist_ok=True)
        os.chmod(self.destination, 0o700)

        archived = []
        for activity in sorted(selection.activities, key=lambda item: item.started_at):
            original = connection.download_activity_original(activity.activity_id)
            fit_payloads = self._fit_payloads(original)
            files = []
            for index, payload in enumerate(fit_payloads, start=1):
                suffix = "" if len(fit_payloads) == 1 else f"-{index}"
                filename = f"{activity.date.isoformat()}_{activity.activity_id}{suffix}.fit"
                status = self._write_fit(filename, payload, overwrite)
                files.append(
                    {
                        "path": filename,
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "status": status,
                    }
                )
            archived.append({**activity.to_dict(), "fit_files": files})

        manifest = {
            "manifest_version": self.MANIFEST_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "selection": selection.to_dict(),
            "activities": archived,
            "analysis_guidance": [
                "Compare pace versus heart rate and perceived session purpose across the selected period.",
                "Inspect interval completion, rep consistency, and heart-rate recovery where FIT records support it.",
                "Inspect long-run continuity, cardiac drift, cadence, and running dynamics where available.",
                "Use symptoms, recovery, blood pressure, heat, and clinician guidance as constraints outside FIT data.",
            ],
        }
        manifest_path = self.destination / "manifest.json"
        self._write_private(manifest_path, json.dumps(manifest, indent=2).encode(), overwrite=True)
        return manifest

    def _write_fit(self, filename, payload, overwrite):
        path = self.destination / filename
        digest = hashlib.sha256(payload).digest()
        if path.exists():
            if hashlib.sha256(path.read_bytes()).digest() == digest:
                return "reused"
            if not overwrite:
                raise FileExistsError(f"Refusing to replace different existing FIT file: {path}")
        self._write_private(path, payload, overwrite=True)
        return "downloaded"

    @staticmethod
    def _write_private(path, payload, overwrite):
        if path.exists() and not overwrite:
            raise FileExistsError(f"Refusing to replace existing file: {path}")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    @classmethod
    def _fit_payloads(cls, original):
        if not isinstance(original, bytes) or not original:
            raise ValueError("Garmin returned an empty or invalid original activity download")

        if zipfile.is_zipfile(io.BytesIO(original)):
            with zipfile.ZipFile(io.BytesIO(original)) as archive:
                candidates = [
                    member
                    for member in archive.infolist()
                    if not member.is_dir() and Path(member.filename).suffix.casefold() == ".fit"
                ]
                if not candidates:
                    raise ValueError("Garmin activity archive contains no FIT file")
                payloads = [archive.read(member) for member in candidates]
        else:
            payloads = [original]

        for payload in payloads:
            cls._validate_fit(payload)
        return payloads

    @staticmethod
    def _validate_fit(payload):
        if len(payload) < 12 or payload[8:12] != b".FIT":
            raise ValueError("Downloaded activity does not have a valid FIT header")


def assessment_date_range(today=None, lookback_days=42):
    if lookback_days < 1:
        raise ValueError("lookback_days must be at least 1")
    end_date = today or date.today()
    return end_date - timedelta(days=lookback_days - 1), end_date
