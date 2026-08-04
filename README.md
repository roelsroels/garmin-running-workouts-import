# Garmin Running Workouts Import

Create structured running workouts from readable YAML, upload them to Garmin Connect, and place them on the Garmin calendar so they sync to a compatible watch. Cycling/FTP workouts from the upstream project remain supported.

> Garmin does not publish the web endpoints used by this tool. Garmin may change them without notice, and authentication can occasionally require maintenance.

## What this fork adds

- Running workouts with warm-up, interval, recovery, rest, cooldown, and other step types.
- Time, distance, or lap-button step endings.
- Pace ranges written naturally as `5:25-5:30` or `["5:25", "5:30"]` per kilometre.
- Explicit repeat groups.
- Dated multi-week plans in one YAML file.
- Preview-first operation: no Garmin login or changes until `--apply` is supplied.
- Create-or-update by workout name and duplicate-safe calendar scheduling.
- Validation for ISO dates and watch-visible name collisions in the first 15 characters.

## Installation

Python 3.10-3.14 and [uv](https://docs.astral.sh/uv/) are required.

```shell
git clone https://github.com/OWNER/garmin-running-workouts-import.git
cd garmin-running-workouts-import
uv sync --dev
```

## Preview a four-week plan

Previewing is offline and does not require Garmin credentials:

```shell
uv run garmin-workouts plan sample_plans/running-4-week-example.yaml
```

The command prints the exact Garmin payloads and makes no remote changes.

## Apply and schedule a plan

Keep credentials local. Never put them in a workout plan, commit them, or share them in chat.

```shell
cp .env.example .env
chmod 600 .env
```

Fill in `GARMIN_USERNAME` and `GARMIN_PASSWORD` in `.env`, then run. The CLI loads this local file automatically:

```shell
uv run garmin-workouts plan sample_plans/running-4-week-example.yaml --apply
```

Applying a plan:

1. Finds existing Garmin workouts by their full name.
2. Updates an existing definition or creates it and captures its Garmin ID.
3. Checks the Garmin calendar month by month.
4. Schedules only workout/date combinations that are not already present.

Use `--upload-only` with `--apply` to create the workout library entries without scheduling them.

After applying, sync Garmin Connect with the watch. A scheduled workout should appear for its calendar day under the watch's running workouts/calendar flow.

## Running workout YAML

```yaml
sport: running
name: "6x2 @ 5:25"
description: "Controlled two-minute repetitions"

steps:
  - { type: warmup, duration: "15:00" }
  - repeat: 6
    steps:
      - { type: interval, duration: "2:00", pace: ["5:25", "5:30"] }
      - { type: recovery, duration: "1:30" }
  - { type: cooldown, duration: "10:00" }
```

Supported running fields:

| Field | Examples | Meaning |
|---|---|---|
| `type` | `warmup`, `interval`, `recovery`, `rest`, `cooldown`, `other` | Garmin step type; defaults to `interval` |
| `duration` | `"2:00"`, `"1:05:00"` | End after time |
| `distance` | `400`, `"400m"`, `"10.3km"` | End after distance |
| `lap_button` | `true` | End when the lap button is pressed |
| `pace` | `"5:25-5:30"` | Pace alert range in min/km |
| `description` | free text | Optional step instruction |

Use only one ending condition per step. Omitting all three ending fields means lap-button press. Pace targets are converted to Garmin's metres-per-second bounds with the faster limit first.

## Dated plan YAML

```yaml
name: "Four-week block"
workouts:
  - date: 2026-08-11
    sport: running
    name: "260811 Q 6x2"
    description: "Quality session"
    steps:
      - { type: warmup, duration: "15:00" }
      - repeat: 6
        steps:
          - { type: interval, duration: "2:00", pace: "5:25-5:30" }
          - { type: recovery, duration: "1:30" }
      - { type: cooldown, duration: "10:00" }
```

Putting `YYMMDD` at the beginning of each name makes a workout easy to identify on the watch. The tool rejects different names that would look identical when only their first 15 characters are visible.

## Import a single workout

```shell
uv run garmin-workouts import sample_workouts/running-6x2.yaml
```

The original cycling format remains available and still requires FTP:

```shell
uv run garmin-workouts import --ftp 250 'sample_workouts/*.yaml'
```

Other upstream commands remain available:

```shell
uv run garmin-workouts list
uv run garmin-workouts get --id WORKOUT_ID
uv run garmin-workouts schedule --date 2026-08-11 --workout_id WORKOUT_ID
uv run garmin-workouts export ./exported-workouts
```

## Four-week FIT-driven workflow

The importer deliberately separates training decisions from delivery:

1. Export and review the latest FIT activities.
2. Generate a dated four-week YAML block from progress, recovery, and current goals.
3. Preview and validate the block locally.
4. Explicitly apply it to Garmin Connect.
5. Reassess before the next block rather than automatically advancing after one activity.

The example plan demonstrates this workflow, but it is not medical clearance. The athlete remains responsible for symptom, blood-pressure, recovery, and clinician-defined stop rules before starting a scheduled workout.

## Security and limitations

- `.env`, `.venv`, and Garmin cookie jars are ignored by Git.
- Use a unique Garmin password and protect `.env` with local file permissions.
- Do not run an unattended cloud job containing Garmin credentials.
- Garmin's private endpoints, MFA, CAPTCHA, or SSO changes can break authentication.
- Reapplying a plan is designed to be repeatable, but deleting or moving old calendar entries remains a manual Garmin Connect action.

## Development

```shell
uv sync --dev
mise run check
```

This fork is based on [mkuthan/garmin-workouts](https://github.com/mkuthan/garmin-workouts) and remains available under the Apache-2.0 license.
