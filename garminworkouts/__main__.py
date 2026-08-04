#!/usr/bin/env python3

import argparse
import glob
import json
import logging
import os

from dotenv import load_dotenv

from garminworkouts.config import configreader
from garminworkouts.garmin.garminclient import GarminClient
from garminworkouts.models.running_workout import RunningWorkout
from garminworkouts.models.training_plan import TrainingPlan
from garminworkouts.plan import PlanApplier, preview_plan
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


def _garmin_client(args):
    if not args.username or not args.password:
        raise ValueError(
            "Garmin credentials are required for this command. Set GARMIN_USERNAME and GARMIN_PASSWORD "
            "or pass --username and --password before the command name."
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
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter, description="Manage Garmin Connect workout(s)"
    )
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

    args = parser.parse_args()

    logging_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=logging_level)

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_usage()


if __name__ == "__main__":
    main()
