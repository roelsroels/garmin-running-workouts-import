from datetime import date, datetime, timedelta

import pytest

from garminworkouts.activities import ActivitySummary
from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.planner import DeterministicPlanner, assess_baseline
from garminworkouts.replanning import nearest_five_minutes
from garminworkouts.state import Goal, PlanRecord


def _activities(count=18):
    start = date(2029, 11, 20)
    activities = []
    for index in range(count):
        activity_date = start + timedelta(days=index * 3)
        distance = 6000 + (index % 3) * 1000
        activities.append(
            ActivitySummary(
                str(index),
                "Run",
                datetime.combine(activity_date, datetime.min.time()),
                "running",
                distance_m=distance,
                duration_s=distance / 2.6,
                average_hr=140,
                max_hr=160,
                average_speed_mps=2.6,
            )
        )
    return activities


def _goal(goal_type="complete_distance"):
    values = {
        "goal_type": goal_type,
        "description": "Generic goal",
        "start_date": date(2030, 1, 8),
        "available_days": (1, 3, 6),
        "long_run_day": 6,
        "runs_per_week": 3,
        "id": 1,
    }
    if goal_type in {"complete_distance", "target_time"}:
        values["target_distance_km"] = 10
    if goal_type == "target_time":
        values["target_time_seconds"] = 3600
    if goal_type == "sustain_pace":
        values["target_pace_seconds_per_km"] = 360
        values["target_duration_minutes"] = 20
    return Goal(**values)


def test_baseline_assigns_confidence_from_history():
    baseline = assess_baseline(_activities())
    assert baseline.confidence == "high"
    assert baseline.longest_run_km == 8
    assert baseline.run_count == 18


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(41, 40), (42, 40), (43, 45), (47, 45), (48, 50)],
)
def test_duration_normalization_rounds_to_nearest_five(minutes, expected):
    assert nearest_five_minutes(minutes) == expected


@pytest.mark.parametrize(("minutes", "expected"), [(41, 40), (43, 45), (48, 50)])
def test_generated_easy_workouts_use_normalized_duration(minutes, expected):
    activity = ActivitySummary(
        "duration-baseline",
        "Run",
        datetime(2030, 1, 1),
        "running",
        distance_m=6000,
        duration_s=minutes * 60,
    )

    proposal = DeterministicPlanner().generate(_goal(), [activity])
    easy = next(item for item in proposal.config["workouts"] if date.fromisoformat(item["date"]).weekday() == 3)

    assert f"Easy{expected}" in easy["name"]
    assert easy["description"].startswith(f"{expected} min easy")


def test_distance_plan_progresses_long_run_without_progressing_quality():
    proposal = DeterministicPlanner().generate(_goal(), _activities())
    TrainingPlan(proposal.config)
    workouts = proposal.config["workouts"]

    assert len(workouts) == 12
    long_runs = [item for item in workouts if "Long" in item["name"]]
    assert [item["steps"][0]["distance"] for item in long_runs] == ["8km", "8km", "8.5km", "7km"]
    quality = [item for item in workouts if " Q " in item["name"]]
    assert [item["name"].split()[-1] for item in quality] == ["4x3", "4x3", "4x3", "3x3"]


def test_long_run_progression_does_not_regress_below_demonstrated_distance():
    activities = [
        *_activities(),
        ActivitySummary(
            "recent-12k",
            "12 km run",
            datetime(2030, 1, 7),
            "running",
            distance_m=12000,
            duration_s=75 * 60,
        ),
    ]

    proposal = DeterministicPlanner().generate(_goal(), activities)
    long_runs = [item for item in proposal.config["workouts"] if "Long" in item["name"]]

    assert [item["steps"][0]["distance"] for item in long_runs] == ["12km", "12km", "12km", "10km"]


def test_target_time_plan_uses_goal_pace_and_extends_quality_duration():
    proposal = DeterministicPlanner().generate(_goal("target_time"), _activities())
    quality = [item for item in proposal.config["workouts"] if " Q " in item["name"]]

    assert quality[0]["steps"][1]["steps"][0]["pace"] == "5:55-6:05"
    assert [item["name"].split()[-1] for item in quality] == ["4x3", "4x3", "3x5", "3x3"]


def test_sustain_pace_plan_progresses_toward_requested_duration():
    proposal = DeterministicPlanner().generate(_goal("sustain_pace"), _activities())
    quality = [item for item in proposal.config["workouts"] if " Q " in item["name"]]

    assert [item["name"].split()[-1] for item in quality] == ["4x3", "3x5", "3x6", "3x4"]


def test_generated_plan_applies_interactive_heart_rate_targets_by_phase():
    goal = Goal(
        goal_type="endurance",
        description="Endurance with HR guidance",
        start_date=date(2030, 1, 8),
        available_days=(1, 3, 6),
        long_run_day=6,
        runs_per_week=3,
        heart_rate_targets={
            "warmup": {"heart_rate_max": 125},
            "easy": {"heart_rate_max": 140},
            "long": {"heart_rate": [120, 145]},
            "quality": {"heart_rate_zone": 4},
            "recovery": {"heart_rate_max": 135},
        },
        quality_target_preference="heart_rate",
        id=2,
    )
    proposal = DeterministicPlanner().generate(goal, _activities())
    TrainingPlan(proposal.config)
    easy = next(item for item in proposal.config["workouts"] if " Easy" in item["name"])
    long_run = next(item for item in proposal.config["workouts"] if " Long" in item["name"])
    quality = next(item for item in proposal.config["workouts"] if " Q " in item["name"])

    assert easy["steps"][0]["heart_rate_max"] == 125
    assert easy["steps"][1]["heart_rate_max"] == 140
    assert long_run["steps"][0]["heart_rate"] == [120, 145]
    assert quality["steps"][1]["steps"][0]["heart_rate_zone"] == 4
    assert quality["steps"][1]["steps"][1]["heart_rate_max"] == 135
    assert all(item["name"].endswith(" HR") for item in (easy, long_run, quality))


def test_pace_can_remain_primary_while_other_phases_use_heart_rate():
    values = _goal("sustain_pace").to_dict()
    values.update(
        {
            "heart_rate_targets": {
                "warmup": {"heart_rate_max": 125},
                "quality": {"heart_rate_max": 165},
            },
            "quality_target_preference": "pace",
        }
    )
    goal = Goal.from_dict(values)
    proposal = DeterministicPlanner().generate(goal, _activities())
    quality = next(item for item in proposal.config["workouts"] if " Q " in item["name"])
    work_step = quality["steps"][1]["steps"][0]

    assert "pace" in work_step
    assert "heart_rate_max" not in work_step
    assert quality["steps"][0]["heart_rate_max"] == 125


def test_adaptation_contains_only_remaining_dates_and_fit_evidence(tmp_path):
    original = DeterministicPlanner().generate(_goal(), _activities())
    record = PlanRecord(
        id=7,
        goal_id=1,
        name="Old",
        start_date=date(2030, 1, 8),
        end_date=date(2030, 2, 3),
        path=tmp_path / "old.yaml",
        config=original.config,
        status="active",
        confidence="high",
        rationale=(),
        created_at="now",
    )
    proposal = DeterministicPlanner().adapt_remaining(
        _goal(),
        record,
        _activities(),
        today=date(2030, 1, 20),
        fit_analysis={"activities": [{"pace_hr_decoupling_percent": 3.2}], "failures": []},
    )

    assert all(date.fromisoformat(item["date"]) >= date(2030, 1, 20) for item in proposal.config["workouts"])
    assert "Decoded 1 original FIT" in " ".join(proposal.rationale)


def test_adaptation_keeps_completed_workouts_immutable_and_missed_workouts_historical(tmp_path):
    original = DeterministicPlanner().generate(_goal(), _activities())
    record = PlanRecord(
        id=7,
        goal_id=1,
        name="Old",
        start_date=date.fromisoformat(original.config["workouts"][0]["date"]),
        end_date=date.fromisoformat(original.config["workouts"][-1]["date"]),
        path=tmp_path / "old.yaml",
        config=original.config,
        status="active",
        confidence="high",
        rationale=(),
        created_at="now",
    )
    today = date(2030, 1, 20)
    future = [item for item in original.config["workouts"] if date.fromisoformat(item["date"]) >= today]
    completed_future = future[0]
    missed_past = next(item for item in original.config["workouts"] if date.fromisoformat(item["date"]) < today)
    progress = [
        {
            "workout_date": completed_future["date"],
            "workout_name": completed_future["name"],
            "status": "completed",
        },
        {
            "workout_date": missed_past["date"],
            "workout_name": missed_past["name"],
            "status": "missed",
        },
    ]

    proposal = DeterministicPlanner().adapt_remaining(_goal(), record, _activities(), today=today, progress=progress)

    proposed_dates = {item["date"] for item in proposal.config["workouts"]}
    assert completed_future["date"] not in proposed_dates
    assert all(date.fromisoformat(item) >= today for item in proposed_dates)
    assert any(reason.startswith("Kept 1 completed workout(s) immutable") for reason in proposal.rationale)
    assert any(reason.startswith("Left 1 missed past workout(s) in history") for reason in proposal.rationale)


def test_adaptation_uses_completed_12k_as_evidence_for_remaining_reduced_week(tmp_path):
    original = DeterministicPlanner().generate(_goal(), _activities())
    record = PlanRecord(
        id=7,
        goal_id=1,
        name="Old",
        start_date=date.fromisoformat(original.config["workouts"][0]["date"]),
        end_date=date.fromisoformat(original.config["workouts"][-1]["date"]),
        path=tmp_path / "old.yaml",
        config=original.config,
        status="active",
        confidence="high",
        rationale=(),
        created_at="now",
    )
    today = date(2030, 1, 27)
    today_workout = next(item for item in original.config["workouts"] if item["date"] == today.isoformat())
    progress = [
        {
            "workout_date": today_workout["date"],
            "workout_name": today_workout["name"],
            "status": "completed",
        }
    ]
    activities = [
        *_activities(),
        ActivitySummary(
            "completed-12k",
            "12 km completed run",
            datetime.combine(today, datetime.min.time()),
            "running",
            distance_m=12000,
            duration_s=75 * 60,
        ),
    ]

    proposal = DeterministicPlanner().adapt_remaining(_goal(), record, activities, today=today, progress=progress)

    assert today_workout["date"] not in {item["date"] for item in proposal.config["workouts"]}
    remaining_long = next(item for item in proposal.config["workouts"] if "Long" in item["name"])
    assert remaining_long["steps"][0]["distance"] == "10km"
    assert any("recent 12.0 km maximum" in reason and "10km" in reason for reason in proposal.rationale)


def test_adaptation_preserves_original_week_numbers_when_evidence_is_unchanged(tmp_path):
    original = DeterministicPlanner().generate(_goal(), _activities())
    record = PlanRecord(
        id=7,
        goal_id=1,
        name="Old",
        start_date=date.fromisoformat(original.config["workouts"][0]["date"]),
        end_date=date.fromisoformat(original.config["workouts"][-1]["date"]),
        path=tmp_path / "old.yaml",
        config=original.config,
        status="active",
        confidence="high",
        rationale=(),
        created_at="now",
    )
    today = date(2030, 1, 20)
    proposal = DeterministicPlanner().adapt_remaining(_goal(), record, _activities(), today=today)
    old_remaining = {
        item["date"]: (item["steps"], item["description"])
        for item in original.config["workouts"]
        if date.fromisoformat(item["date"]) >= today
    }
    new_remaining = {item["date"]: (item["steps"], item["description"]) for item in proposal.config["workouts"]}

    assert new_remaining == old_remaining


def test_next_block_starts_after_finished_plan_and_keeps_goal(tmp_path):
    planner = DeterministicPlanner()
    goal = _goal()
    original = planner.generate(goal, _activities())
    record = PlanRecord(
        id=7,
        goal_id=1,
        name="Finished block",
        start_date=date.fromisoformat(original.config["workouts"][0]["date"]),
        end_date=date.fromisoformat(original.config["workouts"][-1]["date"]),
        path=tmp_path / "finished.yaml",
        config=original.config,
        status="active",
        confidence="high",
        rationale=(),
        created_at="now",
    )
    progress = [
        {
            "workout_date": item["date"],
            "workout_name": item["name"],
            "status": "completed" if index < 10 else "missed",
        }
        for index, item in enumerate(original.config["workouts"])
    ]
    today = record.end_date + timedelta(days=1)

    proposal = planner.generate_next_block(
        goal,
        record,
        _activities(),
        today=today,
        fit_analysis={"activities": [{"id": "fit-1"}], "failures": []},
        progress=progress,
    )

    assert min(date.fromisoformat(item["date"]) for item in proposal.config["workouts"]) >= today
    assert proposal.config["metadata"]["continued_from_plan_id"] == record.id
    assert proposal.config["metadata"]["goal"]["start_date"] == today.isoformat()
    assert proposal.config["metadata"]["previous_block"]["completed"] == 10
    assert proposal.config["metadata"]["previous_block"]["missed"] == 2
    assert proposal.config["metadata"]["fit_assessment"] == {"decoded": 1, "failures": 0}
    assert any("missed workouts were not carried forward" in reason for reason in proposal.rationale)


def test_next_block_rejects_plan_with_upcoming_workouts(tmp_path):
    planner = DeterministicPlanner()
    goal = _goal()
    original = planner.generate(goal, _activities())
    record = PlanRecord(
        id=7,
        goal_id=1,
        name="Active block",
        start_date=date.fromisoformat(original.config["workouts"][0]["date"]),
        end_date=date.fromisoformat(original.config["workouts"][-1]["date"]),
        path=tmp_path / "active.yaml",
        config=original.config,
        status="active",
        confidence="high",
        rationale=(),
        created_at="now",
    )

    with pytest.raises(ValueError, match="still has upcoming workouts"):
        planner.generate_next_block(goal, record, _activities(), today=record.start_date)
