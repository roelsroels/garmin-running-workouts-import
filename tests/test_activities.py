import io
import json
import zipfile
from datetime import date, datetime, timedelta

import pytest

from garminworkouts.activities import (
    ActivityArchive,
    ActivitySummary,
    AssessmentSelector,
    assessment_date_range,
)


def _activity(index, days_ago=0, distance=10000, average_hr=140):
    started = datetime(2026, 8, 1, 9, 30) - timedelta(days=days_ago)
    return ActivitySummary.from_garmin(
        {
            "activityId": index,
            "activityName": f"Run {index}",
            "startTimeLocal": started.isoformat(),
            "activityType": {"typeKey": "running"},
            "distance": distance,
            "duration": 3600,
            "movingDuration": 3500,
            "averageHR": average_hr,
            "maxHR": average_hr + 20,
            "averageSpeed": 2.7777778,
            "elevationGain": 12,
        }
    )


def _fit_payload(marker=b""):
    return b"\x0e\x10\x00\x00\x00\x00\x00\x00.FIT" + marker


def _fit_zip(payload=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("nested/activity.fit", payload or _fit_payload())
    return output.getvalue()


class FakeConnection:
    def __init__(self, payload):
        self.payload = payload
        self.downloaded = []

    def download_activity_original(self, activity_id):
        self.downloaded.append(activity_id)
        return self.payload


def test_activity_summary_normalizes_metrics_and_pace():
    activity = _activity(7)

    assert activity.activity_id == "7"
    assert activity.date == date(2026, 8, 1)
    assert activity.activity_type == "running"
    assert activity.average_pace_seconds_per_km == pytest.approx(360)
    assert activity.to_dict()["average_hr"] == 140


def test_activity_summary_reads_execution_score_when_garmin_lists_it():
    payload = {
        "activityId": 8,
        "activityName": "Structured run",
        "startTimeLocal": "2026-08-01T09:30:00",
        "activityType": {"typeKey": "running"},
        "summaryDTO": {"executionScore": 91},
    }

    activity = ActivitySummary.from_garmin(payload)

    assert activity.execution_score == 91
    assert activity.execution_score_checked is True
    assert activity.to_dict()["execution_score"] == 91


def test_activity_summary_requires_id_and_start_time():
    with pytest.raises(ValueError, match="activityId"):
        ActivitySummary.from_garmin({"startTimeLocal": "2026-08-01T09:00:00"})
    with pytest.raises(ValueError, match="start time"):
        ActivitySummary.from_garmin({"activityId": 7})


def test_selector_uses_recent_window_and_backfills_to_minimum():
    activities = [_activity(index, days_ago=days) for index, days in enumerate((0, 5, 10, 20, 35, 40), 1)]
    selection = AssessmentSelector(window_days=28, minimum_runs=6, maximum_runs=16).select(activities)

    assert selection.recommended_count == 6
    assert selection.coverage_start == date(2026, 6, 22)
    assert "Included older runs" in " ".join(selection.rationale)


def test_selector_caps_busy_window_and_supports_exact_override():
    activities = [_activity(index, days_ago=index) for index in range(20)]
    selector = AssessmentSelector(window_days=28, minimum_runs=6, maximum_runs=16)

    automatic = selector.select(activities)
    exact = selector.select(activities, last=3)

    assert automatic.recommended_count == 16
    assert exact.recommended_count == 3
    assert [activity.activity_id for activity in exact.activities] == ["0", "1", "2"]


def test_selector_validates_configuration_and_empty_input():
    with pytest.raises(ValueError, match="window_days"):
        AssessmentSelector(window_days=0)
    with pytest.raises(ValueError, match="maximum_runs"):
        AssessmentSelector(minimum_runs=5, maximum_runs=4)
    with pytest.raises(ValueError, match="last"):
        AssessmentSelector().select([_activity(1)], last=0)
    assert AssessmentSelector().select([]).recommended_count == 0


def test_archive_extracts_fit_writes_manifest_and_reuses_identical_file(tmp_path):
    activity = _activity(7)
    selection = AssessmentSelector().select([activity], last=1)
    connection = FakeConnection(_fit_zip())
    archive = ActivityArchive(tmp_path / "assessment")

    first = archive.prepare(selection, connection)
    second = archive.prepare(selection, connection)

    fit_path = tmp_path / "assessment" / "2026-08-01_7.fit"
    assert fit_path.read_bytes() == _fit_payload()
    assert first["activities"][0]["fit_files"][0]["status"] == "downloaded"
    assert second["activities"][0]["fit_files"][0]["status"] == "reused"
    manifest = json.loads((tmp_path / "assessment" / "manifest.json").read_text())
    assert manifest["selection"]["recommended_count"] == 1
    guidance = " ".join(manifest["analysis_guidance"]).lower()
    assert "runner's stated goal" in guidance
    assert "blood pressure" not in guidance
    assert "clinician" not in guidance
    assert connection.downloaded == ["7", "7"]


def test_archive_accepts_bare_fit_and_rejects_invalid_download(tmp_path):
    activity = _activity(7)
    selection = AssessmentSelector().select([activity], last=1)

    ActivityArchive(tmp_path / "bare").prepare(selection, FakeConnection(_fit_payload()))
    assert (tmp_path / "bare" / "2026-08-01_7.fit").exists()

    with pytest.raises(ValueError, match="valid FIT header"):
        ActivityArchive(tmp_path / "invalid").prepare(selection, FakeConnection(b"not-fit"))


def test_archive_refuses_to_replace_different_fit_without_overwrite(tmp_path):
    activity = _activity(7)
    selection = AssessmentSelector().select([activity], last=1)
    archive = ActivityArchive(tmp_path / "assessment")
    archive.prepare(selection, FakeConnection(_fit_payload(b"one")))

    with pytest.raises(FileExistsError, match="Refusing to replace"):
        archive.prepare(selection, FakeConnection(_fit_payload(b"two")))

    result = archive.prepare(selection, FakeConnection(_fit_payload(b"two")), overwrite=True)
    assert result["activities"][0]["fit_files"][0]["status"] == "downloaded"


def test_assessment_date_range_is_inclusive():
    assert assessment_date_range(date(2026, 8, 5), 42) == (date(2026, 6, 25), date(2026, 8, 5))
