import json
import math
import os
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

ANALYSIS_VERSION = 1


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _message_value(message, *names):
    for name in names:
        value = message.get(name)
        if value is not None:
            return value
    return None


@dataclass(frozen=True)
class FitActivityMetrics:
    path: str
    start_time: str | None
    sport: str | None
    total_distance_m: float | None
    total_timer_time_s: float | None
    total_elapsed_time_s: float | None
    average_hr: float | None
    max_hr: float | None
    average_speed_mps: float | None
    average_cadence_spm: float | None
    elevation_gain_m: float | None
    record_count: int
    lap_count: int
    pace_hr_decoupling_percent: float | None
    data_quality: tuple[str, ...]

    def to_dict(self):
        return asdict(self)


class FitAnalyzer:
    def decode(self, path):
        try:
            from garmin_fit_sdk import Decoder, Stream
        except ImportError as exc:
            raise RuntimeError("FIT analysis requires the 'garmin-fit-sdk' package") from exc

        stream = Stream.from_file(str(path))
        decoder = Decoder(stream)
        messages, errors = decoder.read()
        fatal = [str(error) for error in errors if error]
        if fatal and not messages:
            raise ValueError(f"Unable to decode FIT file '{path}': {'; '.join(fatal)}")
        return messages, fatal

    def analyze_file(self, path):
        path = Path(path)
        messages, decode_errors = self.decode(path)
        sessions = messages.get("session_mesgs") or []
        records = messages.get("record_mesgs") or []
        laps = messages.get("lap_mesgs") or []
        session = sessions[0] if sessions else {}

        quality = []
        if decode_errors:
            quality.append(f"Decoder reported {len(decode_errors)} recoverable error(s)")
        if not sessions:
            quality.append("No FIT session message")
        if not records:
            quality.append("No FIT record messages")
        if records and not any(_number(record.get("heart_rate")) for record in records):
            quality.append("No record-level heart rate")
        if records and not any(self._record_speed(record) for record in records):
            quality.append("No record-level speed")

        start_time = _message_value(session, "start_time", "timestamp")
        if isinstance(start_time, datetime):
            start_time = start_time.isoformat()
        elif start_time is not None:
            start_time = str(start_time)

        return FitActivityMetrics(
            path=str(path),
            start_time=start_time,
            sport=str(session.get("sport")) if session.get("sport") is not None else None,
            total_distance_m=_number(session.get("total_distance")),
            total_timer_time_s=_number(session.get("total_timer_time")),
            total_elapsed_time_s=_number(session.get("total_elapsed_time")),
            average_hr=_number(_message_value(session, "avg_heart_rate", "average_heart_rate")),
            max_hr=_number(session.get("max_heart_rate")),
            average_speed_mps=_number(_message_value(session, "enhanced_avg_speed", "avg_speed")),
            average_cadence_spm=_number(session.get("avg_running_cadence") or session.get("avg_cadence")),
            elevation_gain_m=_number(session.get("total_ascent")),
            record_count=len(records),
            lap_count=len(laps),
            pace_hr_decoupling_percent=self._decoupling(records),
            data_quality=tuple(quality),
        )

    def analyze_manifest(self, manifest_path, output_path=None):
        manifest_path = Path(manifest_path)
        manifest = json.loads(manifest_path.read_text())
        metrics = []
        failures = []
        for activity in manifest.get("activities", []):
            for fit_file in activity.get("fit_files", []):
                path = manifest_path.parent / fit_file["path"]
                try:
                    result = self.analyze_file(path)
                except Exception as exc:
                    failures.append({"path": str(path), "error": str(exc)})
                    continue
                metrics.append({"activity_id": activity.get("activity_id"), **result.to_dict()})

        analysis = {
            "analysis_version": ANALYSIS_VERSION,
            "manifest": str(manifest_path),
            "activities": metrics,
            "failures": failures,
        }
        output_path = Path(output_path or manifest_path.parent / "analysis.json")
        self._write_private(output_path, json.dumps(analysis, indent=2).encode())
        return analysis

    @staticmethod
    def _record_speed(record):
        return _number(_message_value(record, "enhanced_speed", "speed"))

    @classmethod
    def _decoupling(cls, records):
        samples = []
        for record in records:
            heart_rate = _number(record.get("heart_rate"))
            speed = cls._record_speed(record)
            if heart_rate and heart_rate > 0 and speed and speed > 0:
                samples.append(speed / heart_rate)
        if len(samples) < 20:
            return None
        midpoint = len(samples) // 2
        first = statistics.mean(samples[:midpoint])
        second = statistics.mean(samples[midpoint:])
        if first <= 0:
            return None
        return round((first - second) / first * 100, 2)

    @staticmethod
    def _write_private(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)
