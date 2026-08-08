#!/usr/bin/env python3

import argparse
import glob
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from garminworkouts.activities import (
    ActivityArchive,
    ActivitySummary,
    AssessmentSelector,
    assessment_date_range,
)
from garminworkouts.config import configreader
from garminworkouts.fit_analysis import FitAnalyzer
from garminworkouts.garmin.garminclient import GarminClient
from garminworkouts.garmin.ratelimit import GarminRateLimitError, has_reusable_tokens
from garminworkouts.models.running_workout import RunningWorkout
from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.plan import PlanApplier, preview_plan
from garminworkouts.retire import PlanRetirement
from garminworkouts.state import AppState
from garminworkouts.utils.envdefault import EnvDefault
from garminworkouts.utils.validators import writeable_dir


def command_import(args):
    workout_files = glob.glob(args.workout)
    if not workout_files:
        raise ValueError(f"No workout files match '{args.workout}'")

    workout_configs = [configreader.read_config(workout_file) for workout_file in workout_files]
    workouts = [_running_workout_from_config(config) for config in workout_configs]

    with _garmin_client(args) as connection:
        existing_workouts_by_name = {
            RunningWorkout.extract_workout_name(w): w
            for w in connection.list_workouts()
            if RunningWorkout.is_running(w)
        }

        for workout in workouts:
            workout_name = workout.get_workout_name()
            existing_workout = existing_workouts_by_name.get(workout_name)

            if existing_workout:
                workout_id = RunningWorkout.extract_workout_id(existing_workout)
                workout_owner_id = RunningWorkout.extract_workout_owner_id(existing_workout)
                payload = workout.create_workout(workout_id, workout_owner_id)
                logging.info("Updating workout '%s'", workout_name)
                connection.update_workout(workout_id, payload)
            else:
                payload = workout.create_workout()
                logging.info("Creating workout '%s'", workout_name)
                connection.save_workout(payload)


def command_plan(args):
    plan_config = configreader.read_config(args.plan)
    plan = TrainingPlan(plan_config)

    if not args.apply:
        print(preview_plan(plan))
        return

    with _garmin_client(args) as connection:
        actions = PlanApplier(plan, connection).apply(schedule=not args.upload_only)
    print(json.dumps({"plan": plan.name, "actions": actions}, indent=2))


def command_plan_retire(args):
    plan = TrainingPlan(configreader.read_config(args.plan))
    protected_plans = [TrainingPlan(configreader.read_config(path)) for path in args.protect_plan]

    with _garmin_client(args) as connection:
        retirement = PlanRetirement(plan, connection, protected_plans=protected_plans)
        report = retirement.preview()
        if not args.apply:
            print(json.dumps(report, indent=2))
            return
        actions = retirement.apply(report)

    print(
        json.dumps(
            {
                "plan": plan.name,
                "mode": "applied",
                "actions": actions,
                "completed_activities_deleted": 0,
            },
            indent=2,
        )
    )


def command_export(args):
    with _garmin_client(args) as connection:
        for workout in connection.list_workouts():
            if not RunningWorkout.is_running(workout):
                continue
            workout_id = RunningWorkout.extract_workout_id(workout)
            workout_name = RunningWorkout.extract_workout_name(workout)
            file = os.path.join(args.directory, str(workout_id)) + ".fit"
            logging.info("Exporting workout '%s' into '%s'", workout_name, file)
            connection.download_workout(workout_id, file)


def command_list(args):
    with _garmin_client(args) as connection:
        for workout in connection.list_workouts():
            if not RunningWorkout.is_running(workout):
                continue
            RunningWorkout.print_workout_summary(workout)


def command_schedule(args):
    with _garmin_client(args) as connection:
        workout_id = args.workout_id
        date = args.date
        connection.schedule_workout(workout_id, date)


def command_get(args):
    with _garmin_client(args) as connection:
        workout = connection.get_workout(args.id)
        RunningWorkout.print_workout_json(workout)


def command_delete(args):
    with _garmin_client(args) as connection:
        logging.info("Deleting workout '%s'", args.id)
        connection.delete_workout(args.id)


def command_activities_list(args):
    with _garmin_client(args) as connection:
        activities = _fetch_activity_summaries(connection, args)
    for activity in activities:
        print(_format_activity_summary(activity))


def command_activities_recommend(args):
    with _garmin_client(args) as connection:
        selection = _assessment_selection(connection, args)
    suggested_command = None
    if selection.recommended_count:
        suggested_command = f"./garmin-workouts activities prepare --last {selection.recommended_count}"
    print(
        json.dumps(
            {
                "selection": selection.to_dict(),
                "activities": [activity.to_dict() for activity in selection.activities],
                "suggested_command": suggested_command,
            },
            indent=2,
        )
    )


def command_activities_prepare(args):
    with _garmin_client(args) as connection:
        selection = _assessment_selection(connection, args)
        if not selection.activities:
            raise ValueError("No running activities are available to prepare")
        destination = Path(args.output or f"personal_activities/assessment-{selection.coverage_end.isoformat()}")
        manifest = ActivityArchive(destination).prepare(selection, connection, overwrite=args.overwrite)

    downloaded = sum(
        file["status"] == "downloaded" for activity in manifest["activities"] for file in activity["fit_files"]
    )
    reused = sum(file["status"] == "reused" for activity in manifest["activities"] for file in activity["fit_files"])
    print(
        json.dumps(
            {
                "output": str(destination),
                "manifest": str(destination / "manifest.json"),
                "selection": selection.to_dict(),
                "fit_files_downloaded": downloaded,
                "fit_files_reused": reused,
                "next_step": "Assess manifest.json and its FIT files before generating the next dated training plan.",
            },
            indent=2,
        )
    )


def command_activities_analyze(args):
    analysis = FitAnalyzer().analyze_manifest(args.manifest, args.output)
    print(json.dumps(analysis, indent=2))


def _with_interactive_app(args, action):
    from garminworkouts.app import InteractiveApp

    with AppState(args.data_dir) as state:
        return action(InteractiveApp(state))


def command_app_setup(args):
    _with_interactive_app(args, lambda app: app.setup())


def command_app_status(args):
    _with_interactive_app(args, lambda app: app.show_dashboard())


def command_app_refresh(args):
    _with_interactive_app(args, lambda app: app.refresh())


def command_app_generate(args):
    _with_interactive_app(args, lambda app: app.generate_plan(replace_active=bool(app.state.active_plan())))


def command_app_adapt(args):
    _with_interactive_app(args, lambda app: app.adapt())


def _fetch_activity_summaries(connection, args):
    if args.start_date:
        end_date = args.end_date or date.today().isoformat()
        raw_activities = connection.list_activities_by_date(args.start_date, end_date, args.activity_type)
    elif args.end_date:
        raise ValueError("--to requires --from")
    else:
        raw_activities = connection.list_recent_activities(args.last, args.activity_type)
    return sorted(
        (ActivitySummary.from_garmin(activity) for activity in raw_activities),
        key=lambda activity: activity.started_at,
        reverse=True,
    )


def _assessment_selection(connection, args):
    if args.last is not None:
        if args.last < 1:
            raise ValueError("--last must be at least 1")
        raw_activities = connection.list_recent_activities(args.last, args.activity_type)
    else:
        if args.lookback_days < args.window_days:
            raise ValueError("--lookback-days must be greater than or equal to --window-days")
        start_date, end_date = assessment_date_range(lookback_days=args.lookback_days)
        raw_activities = connection.list_activities_by_date(
            start_date.isoformat(),
            end_date.isoformat(),
            args.activity_type,
        )
    activities = [ActivitySummary.from_garmin(activity) for activity in raw_activities]
    selector = AssessmentSelector(
        window_days=args.window_days,
        minimum_runs=args.minimum_runs,
        maximum_runs=args.maximum_runs,
    )
    return selector.select(activities, last=args.last)


def _format_activity_summary(activity):
    distance = f"{activity.distance_m / 1000:6.2f} km" if activity.distance_m is not None else "       -  "
    duration = _format_duration(activity.duration_s)
    pace = _format_pace(activity.average_pace_seconds_per_km)
    average_hr = f"{activity.average_hr:.0f}" if activity.average_hr is not None else "-"
    max_hr = f"{activity.max_hr:.0f}" if activity.max_hr is not None else "-"
    return (
        f"{activity.started_at:%Y-%m-%d %H:%M}  {activity.activity_id:>12}  {distance}  "
        f"{duration:>8}  {pace:>9}  HR {average_hr:>3}/{max_hr:<3}  {activity.name}"
    )


def _format_duration(seconds):
    if seconds is None:
        return "-"
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _format_pace(seconds_per_km):
    if seconds_per_km is None:
        return "-"
    minutes, seconds = divmod(round(seconds_per_km), 60)
    return f"{minutes}:{seconds:02d}/km"


def _add_assessment_arguments(parser):
    parser.add_argument("--lookback-days", type=int, default=42, help="Candidate activity history to query")
    parser.add_argument(
        "--window-days", type=int, default=28, help="Recent period the automatic selection should cover"
    )
    parser.add_argument("--minimum-runs", type=int, default=6, help="Backfill older runs until this sample size")
    parser.add_argument("--maximum-runs", type=int, default=16, help="Maximum automatic sample size")
    parser.add_argument("--last", type=int, help="Override automatic selection with exactly the last N available runs")
    parser.add_argument("--type", dest="activity_type", default="running", help="Activity type")


def _garmin_client(args):
    token_store = Path(args.token_store).expanduser()
    has_tokens = has_reusable_tokens(token_store)
    if not args.username or (not args.password and not has_tokens):
        raise ValueError(
            "Garmin credentials or a reusable token session are required. Set GARMIN_USERNAME and GARMIN_PASSWORD, "
            "pass --username and --password before the command name, or use the interactive setup."
        )
    return GarminClient(
        username=args.username,
        password=args.password,
        token_store=args.token_store,
    )


def _running_workout_from_config(config):
    sport = config.get("sport", "running")
    if sport != "running":
        raise ValueError(f"Unsupported sport '{sport}'; this tool supports running workouts only")
    return RunningWorkout(config)


def main():
    load_dotenv()
    # Never use ArgumentDefaultsHelpFormatter here: legacy credentials can be
    # sourced from environment variables, and secret defaults must not appear
    # in --help output.
    parser = argparse.ArgumentParser(description="Manage Garmin Connect workout(s)")
    parser.add_argument(
        "--username",
        "-u",
        action=EnvDefault,
        env_var="GARMIN_USERNAME",
        required=False,
        help="Garmin Connect account username",
    )
    parser.add_argument(
        "--password",
        "-p",
        action=EnvDefault,
        env_var="GARMIN_PASSWORD",
        required=False,
        help="Garmin Connect account password",
    )
    parser.add_argument("--token-store", default=".garmin-tokens", help="Directory for Garmin authentication tokens")
    parser.add_argument(
        "--data-dir",
        default=os.getenv("GARMIN_WORKOUTS_HOME", "~/.garmin-running-workouts"),
        help="Portable local application state directory",
    )
    parser.add_argument("--debug", action="store_true", help="Enables more detailed messages")

    subparsers = parser.add_subparsers(title="Commands")

    parser_import = subparsers.add_parser("import", description="Import workout(s) from file(s) into Garmin Connect")
    parser_import.add_argument(
        "workout", help="File(s) with workout(s) to import, wildcards are supported e.g: sample_workouts/*.yaml"
    )
    parser_import.set_defaults(func=command_import)

    parser_plan = subparsers.add_parser(
        "plan",
        description="Preview or apply a dated training plan. Preview is the safe default; use --apply to upload.",
    )
    parser_plan.add_argument("plan", help="YAML file containing dated workout definitions")
    parser_plan.add_argument("--apply", action="store_true", help="Create/update workouts and schedule them")
    parser_plan.add_argument(
        "--upload-only", action="store_true", help="Create/update workouts but do not place them on the calendar"
    )
    parser_plan.set_defaults(func=command_plan)

    parser_plan_retire = subparsers.add_parser(
        "plan-retire",
        description=(
            "Preview or retire a dated plan by unscheduling its future calendar entries and deleting its workout "
            "templates. Preview is the safe default."
        ),
    )
    parser_plan_retire.add_argument("plan", help="Old dated plan YAML to retire")
    parser_plan_retire.add_argument(
        "--protect-plan",
        action="append",
        default=[],
        help="New/active plan YAML whose reused workout names and calendar entries must be retained; repeatable",
    )
    parser_plan_retire.add_argument("--apply", action="store_true", help="Perform the previewed Garmin deletions")
    parser_plan_retire.set_defaults(func=command_plan_retire)

    parser_export = subparsers.add_parser(
        "export", description="Export all workouts from Garmin Connect and save into directory"
    )
    parser_export.add_argument(
        "directory", type=writeable_dir, help="Destination directory where workout(s) will be exported"
    )
    parser_export.set_defaults(func=command_export)

    parser_list = subparsers.add_parser("list", description="List all workouts")
    parser_list.set_defaults(func=command_list)

    parser_schedule = subparsers.add_parser("schedule", description="Schedule a workouts")
    parser_schedule.add_argument("--workout_id", "-w", required=True, help="Workout id to schedule")
    parser_schedule.add_argument("--date", "-d", required=True, help="Date to which schedule the workout")
    parser_schedule.set_defaults(func=command_schedule)

    parser_get = subparsers.add_parser("get", description="Get workout")
    parser_get.add_argument("--id", required=True, help="Workout id, use list command to get workouts identifiers")
    parser_get.set_defaults(func=command_get)

    parser_delete = subparsers.add_parser("delete", description="Delete workout")
    parser_delete.add_argument("--id", required=True, help="Workout id, use list command to get workouts identifiers")
    parser_delete.set_defaults(func=command_delete)

    parser_activities = subparsers.add_parser(
        "activities",
        description="List completed activities or prepare original FIT files for assessment",
    )
    activity_subparsers = parser_activities.add_subparsers(title="Activity commands")

    parser_activities_list = activity_subparsers.add_parser(
        "list", description="List completed activities newest first"
    )
    parser_activities_list.add_argument("--from", dest="start_date", help="First activity date (YYYY-MM-DD)")
    parser_activities_list.add_argument("--to", dest="end_date", help="Last activity date (YYYY-MM-DD)")
    parser_activities_list.add_argument(
        "--last", type=int, default=20, help="Number of recent activities without dates"
    )
    parser_activities_list.add_argument("--type", dest="activity_type", default="running", help="Activity type")
    parser_activities_list.set_defaults(func=command_activities_list)

    parser_activities_recommend = activity_subparsers.add_parser(
        "recommend", description="Recommend a useful recent FIT assessment set without downloading it"
    )
    _add_assessment_arguments(parser_activities_recommend)
    parser_activities_recommend.set_defaults(func=command_activities_recommend)

    parser_activities_prepare = activity_subparsers.add_parser(
        "prepare", description="Recommend and download a private FIT assessment set with a manifest"
    )
    _add_assessment_arguments(parser_activities_prepare)
    parser_activities_prepare.add_argument(
        "--output", help="Private output directory; defaults to personal_activities/assessment-DATE"
    )
    parser_activities_prepare.add_argument(
        "--overwrite", action="store_true", help="Replace a different local FIT file with the same activity ID"
    )
    parser_activities_prepare.set_defaults(func=command_activities_prepare)

    parser_activities_analyze = activity_subparsers.add_parser(
        "analyze", description="Decode a prepared FIT manifest with Garmin's official FIT SDK"
    )
    parser_activities_analyze.add_argument("manifest", help="Path to a prepared manifest.json")
    parser_activities_analyze.add_argument("--output", help="Output analysis.json path")
    parser_activities_analyze.set_defaults(func=command_activities_analyze)

    parser_setup = subparsers.add_parser("setup", description="Run the interactive first-time setup")
    parser_setup.set_defaults(func=command_app_setup)

    parser_status = subparsers.add_parser("status", description="Show the active goal and locally stored plan progress")
    parser_status.set_defaults(func=command_app_status)

    parser_refresh = subparsers.add_parser("refresh", description="Refresh completed runs for the active plan")
    parser_refresh.set_defaults(func=command_app_refresh)

    parser_generate = subparsers.add_parser("generate", description="Generate and review a goal-driven training plan")
    parser_generate.set_defaults(func=command_app_generate)

    parser_adapt = subparsers.add_parser("adapt", description="Assess and propose changes to remaining scheduled days")
    parser_adapt.set_defaults(func=command_app_adapt)

    args = parser.parse_args()

    logging_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=logging_level)

    try:
        if hasattr(args, "func"):
            args.func(args)
        elif sys.stdin.isatty():
            _with_interactive_app(args, lambda app: app.run())
        else:
            parser.print_help()
    except GarminRateLimitError as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
