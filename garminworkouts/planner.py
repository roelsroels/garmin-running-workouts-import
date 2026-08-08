import math
import os
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

ENGINE_VERSION = 2


@dataclass(frozen=True)
class TrainingBaseline:
    run_count: int
    coverage_days: int
    runs_per_week: float
    median_duration_minutes: float | None
    median_distance_km: float | None
    longest_run_km: float | None
    median_pace_seconds_per_km: float | None
    confidence: str

    def to_dict(self):
        return {
            "run_count": self.run_count,
            "coverage_days": self.coverage_days,
            "runs_per_week": round(self.runs_per_week, 2),
            "median_duration_minutes": _rounded(self.median_duration_minutes),
            "median_distance_km": _rounded(self.median_distance_km),
            "longest_run_km": _rounded(self.longest_run_km),
            "median_pace_seconds_per_km": _rounded(self.median_pace_seconds_per_km),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class PlanProposal:
    config: dict
    baseline: TrainingBaseline
    confidence: str
    rationale: tuple[str, ...]


def _rounded(value, digits=2):
    return round(value, digits) if value is not None else None


def assess_baseline(activities):
    activities = sorted(activities, key=lambda item: item.started_at)
    if not activities:
        return TrainingBaseline(0, 0, 0.0, None, None, None, None, "insufficient")

    coverage_days = max(1, (activities[-1].date - activities[0].date).days + 1)
    durations = [activity.duration_s / 60 for activity in activities if activity.duration_s and activity.duration_s > 0]
    distances = [
        activity.distance_m / 1000 for activity in activities if activity.distance_m and activity.distance_m > 0
    ]
    paces = [
        activity.average_pace_seconds_per_km
        for activity in activities
        if activity.average_pace_seconds_per_km and (activity.duration_s or 0) >= 15 * 60
    ]
    runs_per_week = len(activities) / max(1.0, coverage_days / 7)
    if len(activities) < 6 or coverage_days < 21:
        confidence = "insufficient"
    elif len(activities) < 12 or coverage_days < 35:
        confidence = "moderate"
    else:
        confidence = "high"
    return TrainingBaseline(
        run_count=len(activities),
        coverage_days=coverage_days,
        runs_per_week=runs_per_week,
        median_duration_minutes=statistics.median(durations) if durations else None,
        median_distance_km=statistics.median(distances) if distances else None,
        longest_run_km=max(distances) if distances else None,
        median_pace_seconds_per_km=statistics.median(paces) if paces else None,
        confidence=confidence,
    )


class DeterministicPlanner:
    def generate(self, goal, activities):
        baseline = assess_baseline(activities)
        selected_days, actual_frequency, frequency_rationale = self._selected_days(goal, baseline)
        dates = self._schedule_dates(goal.start_date, goal.plan_weeks, selected_days)
        rationale = list(frequency_rationale)
        if goal.target_date:
            dates = [item for item in dates if item[0] <= goal.target_date]
            if not dates:
                raise ValueError("No available running day falls between the plan start date and target date")
        if goal.constraints:
            rationale.append(
                "User-supplied constraints are preserved for review but are not automatically "
                "interpreted by the engine."
            )
        if goal.heart_rate_targets:
            rationale.append(
                "Heart-rate alerts are copied from user-supplied targets; the engine does not derive or "
                "clinically validate BPM limits or Garmin zones."
            )

        long_run_km = baseline.longest_run_km or goal.baseline_long_run_km
        if long_run_km is None:
            long_run_km = min(goal.target_distance_km or 6.0, 6.0)
            rationale.append(
                "No usable distance history was available; the long-run baseline uses a conservative default."
            )
        elif baseline.longest_run_km:
            rationale.append(f"Long-run construction is anchored to the recent {long_run_km:.1f} km maximum.")

        easy_minutes = round(baseline.median_duration_minutes or 35)
        easy_minutes = max(20, min(easy_minutes, goal.max_session_minutes, 60))
        if baseline.confidence == "insufficient":
            rationale.append(
                "History is insufficient for narrow targets; the proposal emphasizes familiar easy running."
            )
        else:
            rationale.append("The proposal uses repeated exposure before progression and a reduced final week.")

        quality_day = self._quality_day(selected_days, goal.long_run_day, goal.goal_type, actual_frequency)
        workouts = []
        for workout_date, week_index in dates:
            if workout_date.weekday() == goal.long_run_day:
                workout = self._long_workout(goal, workout_date, week_index, long_run_km)
            elif workout_date.weekday() == quality_day:
                workout = self._quality_workout(goal, workout_date, week_index, baseline.confidence)
            else:
                workout = self._easy_workout(workout_date, week_index, easy_minutes, goal=goal)
            workouts.append(workout)

        confidence = baseline.confidence
        config = {
            "name": f"{goal.start_date.isoformat()} {goal.plan_weeks}-week {goal.goal_type.replace('_', ' ')} block",
            "metadata": {
                "engine": "deterministic",
                "engine_version": ENGINE_VERSION,
                "goal_id": goal.id,
                "goal": goal.to_dict(),
                "baseline": baseline.to_dict(),
                "confidence": confidence,
                "rationale": rationale,
            },
            "workouts": workouts,
        }
        return PlanProposal(config, baseline, confidence, tuple(rationale))

    def adapt_remaining(self, goal, active_plan, activities, today=None, fit_analysis=None):
        today = today or date.today()
        baseline = assess_baseline(activities)
        selected_days, actual_frequency, frequency_rationale = self._selected_days(goal, baseline)
        remaining_dates = sorted(
            date.fromisoformat(str(item["date"]))
            for item in active_plan.config.get("workouts", [])
            if date.fromisoformat(str(item["date"])) >= today
            and date.fromisoformat(str(item["date"])).weekday() in selected_days
        )
        if not remaining_dates:
            raise ValueError("The active plan has no remaining scheduled days to adapt")

        easy_minutes = round(baseline.median_duration_minutes or 35)
        easy_minutes = max(20, min(easy_minutes, goal.max_session_minutes, 60))
        long_run_km = baseline.longest_run_km or goal.baseline_long_run_km or min(goal.target_distance_km or 6.0, 6.0)
        quality_day = self._quality_day(selected_days, goal.long_run_day, goal.goal_type, actual_frequency)

        workouts = []
        for workout_date in remaining_dates:
            # Keep each workout in its original block week. Re-numbering the first
            # remaining day as week zero would manufacture changes even when the
            # new evidence supports retaining the current schedule.
            week_index = min(
                goal.plan_weeks - 1,
                max(0, (workout_date - active_plan.start_date).days // 7),
            )
            if workout_date.weekday() == goal.long_run_day:
                workout = self._long_workout(goal, workout_date, week_index, long_run_km)
            elif workout_date.weekday() == quality_day:
                workout = self._quality_workout(goal, workout_date, week_index, baseline.confidence)
            else:
                workout = self._easy_workout(workout_date, week_index, easy_minutes, goal=goal)
            workouts.append(workout)

        confidence = baseline.confidence
        fit_rationale = []
        if fit_analysis is not None:
            decoded = len(fit_analysis.get("activities", []))
            failed = len(fit_analysis.get("failures", []))
            fit_rationale.append(f"Decoded {decoded} original FIT file(s); {failed} file(s) could not be decoded.")
            if decoded == 0:
                confidence = "insufficient"
                fit_rationale.append("No decoded FIT evidence was available, so adaptation confidence was reduced.")
            else:
                decoupling = [
                    item.get("pace_hr_decoupling_percent")
                    for item in fit_analysis.get("activities", [])
                    if item.get("pace_hr_decoupling_percent") is not None
                ]
                if decoupling:
                    fit_rationale.append(
                        "Pace-to-heart-rate decoupling was calculated as descriptive context for "
                        f"{len(decoupling)} run(s), not as a standalone progression rule."
                    )

        rationale = [
            *frequency_rationale,
            f"Reassessed {baseline.run_count} recent runs and rebuilt only {len(workouts)} remaining scheduled days.",
            *fit_rationale,
            "Completed plan dates are excluded from the replacement proposal.",
        ]
        config = {
            "name": f"{today.isoformat()} adapted {goal.goal_type.replace('_', ' ')} block",
            "metadata": {
                "engine": "deterministic",
                "engine_version": ENGINE_VERSION,
                "goal_id": goal.id,
                "adapted_from_plan_id": active_plan.id,
                "goal": goal.to_dict(),
                "baseline": baseline.to_dict(),
                "confidence": confidence,
                "rationale": rationale,
            },
            "workouts": workouts,
        }
        return PlanProposal(config, baseline, confidence, tuple(rationale))

    @staticmethod
    def _selected_days(goal, baseline):
        requested = min(goal.runs_per_week, len(goal.available_days))
        rationale = []
        if baseline.run_count >= 4:
            demonstrated = max(1, math.ceil(baseline.runs_per_week))
            actual = min(requested, demonstrated + 1)
            if actual < requested:
                rationale.append(
                    f"Requested frequency was capped at {actual} runs per week, one above the recent demonstrated "
                    f"frequency of approximately {baseline.runs_per_week:.1f}."
                )
        else:
            actual = min(requested, 3)
            if actual < requested:
                rationale.append("Sparse history limits the first proposal to three runs per week.")

        chosen = [goal.long_run_day]
        for day in goal.available_days:
            if day != goal.long_run_day and len(chosen) < actual:
                chosen.append(day)
        return tuple(sorted(chosen)), actual, tuple(rationale)

    @staticmethod
    def _schedule_dates(start_date, weeks, selected_days):
        result = []
        for week_index in range(weeks):
            for weekday in selected_days:
                first_offset = (weekday - start_date.weekday()) % 7
                result.append((start_date + timedelta(days=first_offset + 7 * week_index), week_index))
        return sorted(result)

    @staticmethod
    def _quality_day(selected_days, long_run_day, goal_type, frequency):
        if frequency < 3 or goal_type == "consistency":
            return None
        candidates = [day for day in selected_days if day != long_run_day]
        return candidates[0] if candidates else None

    def _easy_workout(self, workout_date, week_index, easy_minutes, goal=None):
        duration = easy_minutes if week_index < 3 else max(20, round(easy_minutes * 0.85))
        steps = [
            self._heart_rate_step(goal, "warmup", {"type": "warmup", "duration": "5:00"}),
            self._heart_rate_step(
                goal,
                "easy",
                {"type": "interval", "duration": _minutes(max(10, duration - 10))},
            ),
            self._heart_rate_step(goal, "warmup", {"type": "cooldown", "duration": "5:00"}),
        ]
        name = f"{workout_date:%y%m%d} Easy{duration}{_heart_rate_name_suffix(steps)}"
        heart_rate_text = _heart_rate_description(self._heart_rate_target(goal, "easy"))
        return {
            "date": workout_date.isoformat(),
            "sport": "running",
            "name": name,
            "description": (
                f"{duration} min easy; RPE 2-3 and conversational; no pace target{_description_suffix(heart_rate_text)}"
            ),
            "steps": steps,
        }

    def _long_workout(self, goal, workout_date, week_index, baseline_km):
        if goal.goal_type in {"complete_distance", "endurance", "consistency"} and week_index == 2:
            distance = baseline_km * 1.05
            if goal.target_distance_km:
                distance = min(distance, goal.target_distance_km)
        elif week_index >= 3:
            distance = baseline_km * 0.85
        else:
            distance = baseline_km
        distance = max(3.0, round(distance * 2) / 2)
        code = round(distance * 10)
        steps = [self._heart_rate_step(goal, "long", {"type": "interval", "distance": f"{distance:g}km"})]
        heart_rate_text = _heart_rate_description(self._heart_rate_target(goal, "long"))
        return {
            "date": workout_date.isoformat(),
            "sport": "running",
            "name": f"{workout_date:%y%m%d} Long{code:03d}{_heart_rate_name_suffix(steps)}",
            "description": (f"{distance:g} km easy; RPE 2-3 and conversational{_description_suffix(heart_rate_text)}"),
            "steps": steps,
        }

    def _quality_workout(self, goal, workout_date, week_index, confidence):
        if confidence == "insufficient":
            return self._easy_workout(workout_date, week_index, 30, goal=goal)
        if goal.goal_type == "sustain_pace":
            fractions = (0.60, 0.75, 0.90, 0.65)
            repetitions = 2 if goal.target_duration_minutes <= 10 else (4 if week_index == 0 else 3)
            work_minutes = max(
                2,
                round(goal.target_duration_minutes * fractions[min(week_index, 3)] / repetitions),
            )
        elif week_index == 2 and goal.goal_type in {"target_time", "speed"}:
            repetitions, work_minutes = 3, 5
        elif week_index >= 3:
            repetitions, work_minutes = 3, 3
        else:
            repetitions, work_minutes = 4, 3
        target_pace = goal.target_pace
        work_step = {"type": "interval", "duration": _minutes(work_minutes)}
        target_text = "at controlled RPE 6-7"
        use_quality_heart_rate = goal.quality_target_preference == "heart_rate" and self._heart_rate_target(
            goal, "quality"
        )
        if target_pace and not use_quality_heart_rate:
            pace_range = _pace_range(target_pace)
            work_step["pace"] = pace_range
            target_text = f"at {pace_range}/km, capped at RPE 7"
        else:
            work_step = self._heart_rate_step(goal, "quality", work_step)
            heart_rate_text = _heart_rate_description(self._heart_rate_target(goal, "quality"))
            if heart_rate_text:
                target_text = f"using {heart_rate_text}, capped at RPE 7"
        steps = [
            self._heart_rate_step(goal, "warmup", {"type": "warmup", "duration": "15:00"}),
            {
                "repeat": repetitions,
                "steps": [
                    work_step,
                    self._heart_rate_step(goal, "recovery", {"type": "recovery", "duration": "1:30"}),
                ],
            },
            self._heart_rate_step(goal, "warmup", {"type": "cooldown", "duration": "10:00"}),
        ]
        return {
            "date": workout_date.isoformat(),
            "sport": "running",
            "name": (f"{workout_date:%y%m%d} Q {repetitions}x{work_minutes}{_heart_rate_name_suffix(steps)}"),
            "description": (
                f"15 min easy; {repetitions} x {work_minutes} min {target_text} with 90 sec easy; 10 min easy"
            ),
            "steps": steps,
        }

    @staticmethod
    def _heart_rate_target(goal, phase):
        if goal is None or not goal.heart_rate_targets:
            return None
        target = goal.heart_rate_targets.get(phase)
        if target is None and phase in {"warmup", "recovery", "long"}:
            target = goal.heart_rate_targets.get("easy")
        return target

    def _heart_rate_step(self, goal, phase, step):
        target = self._heart_rate_target(goal, phase)
        if target and "pace" not in step:
            return {**step, **target}
        return step


def _minutes(minutes):
    return f"{int(minutes)}:00"


def _pace(seconds):
    minutes, remainder = divmod(max(120, int(seconds)), 60)
    return f"{minutes}:{remainder:02d}"


def _pace_range(target_seconds):
    return f"{_pace(target_seconds - 5)}-{_pace(target_seconds + 5)}"


def _heart_rate_description(target):
    if not target:
        return ""
    if "heart_rate_max" in target:
        return f"HR ≤{target['heart_rate_max']} bpm"
    if "heart_rate" in target:
        lower, upper = target["heart_rate"]
        return f"HR {lower}-{upper} bpm"
    return f"Garmin HR zone {target['heart_rate_zone']}"


def _description_suffix(text):
    return f"; {text}" if text else ""


def _heart_rate_name_suffix(steps):
    def contains_target(items):
        for item in items:
            if any(key in item for key in ("heart_rate_max", "heart_rate", "heart_rate_zone")):
                return True
            if contains_target(item.get("steps", [])):
                return True
        return False

    return " HR" if contains_target(steps) else ""


def write_plan(path, config):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)
    return path
