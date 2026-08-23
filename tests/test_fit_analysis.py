import json
from pathlib import Path

from garminworkouts.activities import ActivitySummary
from garminworkouts.fit_analysis import FitActivityMetrics, FitAnalyzer


def _fit_payload():
    return b"\x0e\x10\x00\x00\x00\x00\x00\x00.FIT\x00"


def test_fit_analyzer_derives_session_metrics_and_decoupling(monkeypatch, tmp_path):
    records = []
    for index in range(20):
        records.append({"heart_rate": 140 + index / 10, "enhanced_speed": 3.0 - index / 200})
    messages = {
        "session_mesgs": [
            {
                "sport": "running",
                "total_distance": 10000,
                "total_timer_time": 3600,
                "total_elapsed_time": 3650,
                "avg_heart_rate": 145,
                "max_heart_rate": 165,
                "enhanced_avg_speed": 2.78,
                "avg_running_cadence": 170,
                "total_ascent": 20,
            }
        ],
        "record_mesgs": records,
        "lap_mesgs": [{}, {}],
    }
    analyzer = FitAnalyzer()
    monkeypatch.setattr(analyzer, "decode", lambda path: (messages, []))

    result = analyzer.analyze_file(tmp_path / "run.fit")

    assert result.total_distance_m == 10000
    assert result.average_hr == 145
    assert result.record_count == 20
    assert result.lap_count == 2
    assert result.pace_hr_decoupling_percent is not None


def test_fit_analyzer_reads_undocumented_garmin_execution_score(monkeypatch):
    analyzer = FitAnalyzer()
    monkeypatch.setattr(analyzer, "decode_payload", lambda payload: {"session_mesgs": [{185: 87}]})

    score = analyzer.execution_score_from_original(_fit_payload())

    assert score == 87


def test_execution_score_enrichment_downloads_each_completed_activity_once(monkeypatch):
    activity = ActivitySummary.from_garmin(
        {
            "activityId": 42,
            "activityName": "Structured run",
            "startTimeLocal": "2026-08-20T09:30:00",
            "activityType": {"typeKey": "running"},
        }
    )

    class Connection:
        def __init__(self):
            self.downloaded = []

        def download_activity_original(self, activity_id):
            self.downloaded.append(activity_id)
            return _fit_payload()

    connection = Connection()
    analyzer = FitAnalyzer()
    monkeypatch.setattr(analyzer, "execution_score_from_original", lambda payload: 93)

    enriched, errors = analyzer.enrich_execution_scores(
        [activity],
        {activity.date},
        set(),
        connection,
    )
    cached, cached_errors = analyzer.enrich_execution_scores(
        [activity],
        {activity.date},
        {activity.activity_id},
        connection,
    )

    assert errors == []
    assert enriched[0].execution_score == 93
    assert enriched[0].execution_score_checked is True
    assert cached == [activity]
    assert cached_errors == []
    assert connection.downloaded == ["42"]


def test_execution_score_enrichment_uses_same_day_match_as_progress(monkeypatch):
    first = ActivitySummary.from_garmin(
        {
            "activityId": 1,
            "activityName": "Morning run",
            "startTimeLocal": "2026-08-20T08:00:00",
            "activityType": {"typeKey": "running"},
        }
    )
    second = ActivitySummary.from_garmin(
        {
            "activityId": 2,
            "activityName": "Evening run",
            "startTimeLocal": "2026-08-20T18:00:00",
            "activityType": {"typeKey": "running"},
        }
    )

    class Connection:
        def __init__(self):
            self.downloaded = []

        def download_activity_original(self, activity_id):
            self.downloaded.append(activity_id)
            return _fit_payload()

    connection = Connection()
    analyzer = FitAnalyzer()
    monkeypatch.setattr(analyzer, "execution_score_from_original", lambda payload: 80)

    enriched, errors = analyzer.enrich_execution_scores(
        [second, first],
        {first.date},
        set(),
        connection,
    )

    assert errors == []
    assert connection.downloaded == ["1"]
    assert [activity.execution_score for activity in enriched] == [None, 80]


def test_manifest_analysis_records_success_and_failure(monkeypatch, tmp_path):
    manifest = {
        "activities": [
            {"activity_id": "1", "fit_files": [{"path": "one.fit"}]},
            {"activity_id": "2", "fit_files": [{"path": "two.fit"}]},
        ]
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    analyzer = FitAnalyzer()
    metric = FitActivityMetrics(
        "one.fit", None, "running", None, None, None, None, None, None, None, None, 0, 0, None, ()
    )

    def analyze(path):
        if Path(path).name == "two.fit":
            raise ValueError("broken")
        return metric

    monkeypatch.setattr(analyzer, "analyze_file", analyze)
    result = analyzer.analyze_manifest(manifest_path)

    assert len(result["activities"]) == 1
    assert result["activities"][0]["activity_id"] == "1"
    assert len(result["failures"]) == 1
    assert (tmp_path / "analysis.json").exists()
