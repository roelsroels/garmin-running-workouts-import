import json
from pathlib import Path

from garminworkouts.fit_analysis import FitActivityMetrics, FitAnalyzer


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
