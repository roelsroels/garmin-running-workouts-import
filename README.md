# Garmin Running Workouts Import

Create structured running workouts from readable YAML, upload them to Garmin Connect, and place them on the Garmin calendar so they sync to a compatible watch. This fork is intentionally running-only; use the upstream project for other sports.

> Garmin does not publish the web endpoints used by this tool. Garmin may change them without notice, and authentication can occasionally require maintenance. This fork uses the actively maintained `garminconnect` client for the current authenticated API.

## Features

- Running workouts with warm-up, interval, recovery, rest, cooldown, and other step types.
- Time, distance, or lap-button step endings.
- Pace ranges written naturally as `5:25-5:30` or `["5:25", "5:30"]` per kilometre.
- Heart-rate caps, custom BPM ranges, and Garmin heart-rate zones.
- Explicit repeat groups.
- Dated multi-week plans in one YAML file.
- Preview-first operation: no Garmin login or changes until `--apply` is supplied.
- Create-or-update by workout name and duplicate-safe calendar scheduling.
- Validation for ISO dates and watch-visible name collisions in the first 15 characters.
- Chronological listing of completed Garmin activities with distance, duration, pace, and heart rate.
- Adaptive selection and automatic download of private original FIT assessment bundles.
- Versioned JSON manifests designed for chat-assisted analysis and future desktop/mobile clients.

## Installation

Python 3.12-3.14 and [uv](https://docs.astral.sh/uv/) are required.

```shell
git clone https://github.com/OWNER/garmin-running-workouts-import.git
cd garmin-running-workouts-import
uv sync --dev
```

Use the repository launcher for every command. It runs the source directly and
does not depend on Python editable-install loaders:

```shell
./garmin-workouts --help
```

## Preview a four-week plan

Previewing is offline and does not require Garmin credentials:

```shell
./garmin-workouts plan sample_plans/running-4-week-example.yaml
```

The command prints the exact Garmin payloads and makes no remote changes.

### Keep personalized plans local

The `personal_plans/` directory is ignored by Git so dated schedules, health-related
instructions, and training details are not published with the repository. Create or
place a plan there, then use the same preview-first workflow:

```shell
./garmin-workouts plan personal_plans/my-four-week-plan.yaml
./garmin-workouts plan personal_plans/my-four-week-plan.yaml --apply
```

## Apply and schedule a plan

Keep credentials local. Never put them in a workout plan, commit them, or share them in chat.

```shell
cp .env.example .env
chmod 600 .env
```

Fill in `GARMIN_USERNAME` and `GARMIN_PASSWORD` in `.env`, then run. The CLI loads this local file automatically:

```shell
./garmin-workouts plan sample_plans/running-4-week-example.yaml --apply
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
| `heart_rate_max` | `120` | Practical upper cap, represented as Garmin's `35-120 bpm` target range |
| `heart_rate` | `[110, 130]` | Custom lower/upper BPM target range |
| `heart_rate_zone` | `2` | Garmin running heart-rate zone 1-5 |
| `description` | free text | Optional step instruction |

Use only one ending condition and one intensity target per step. Omitting all three ending fields means lap-button press. Pace targets are converted to Garmin's metres-per-second bounds with the faster limit first.

### Heart-rate cap example

Garmin workout intensity targets are ranges rather than one-sided limits. `heart_rate_max` uses Garmin's 35 bpm custom-workout floor as the lower boundary, making it an effective upper-cap alert during a normal run:

```yaml
sport: running
name: "HR cap progression"
steps:
  - { type: warmup, duration: "10:00", heart_rate_max: 120 }
  - { type: interval, duration: "15:00", heart_rate_max: 140 }
  - { type: cooldown, duration: "10:00", heart_rate_max: 120 }
```

Use `heart_rate: [120, 140]` when the watch should alert at both the lower and upper boundary. Use `heart_rate_zone: 2` when the workout should follow the running HR zones currently configured in Garmin.

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
./garmin-workouts import sample_workouts/running-6x2.yaml
```

Other upstream commands remain available:

```shell
./garmin-workouts list
./garmin-workouts get --id WORKOUT_ID
./garmin-workouts schedule --date 2026-08-11 --workout_id WORKOUT_ID
./garmin-workouts export ./exported-workouts
```

## Download completed running activities

The older `export` command above exports saved workout definitions. Completed activities recorded by the watch use the separate `activities` commands.

List the latest completed runs, newest first:

```shell
./garmin-workouts activities list --last 20
```

Or list a date range:

```shell
./garmin-workouts activities list --from 2026-07-01 --to 2026-08-05
```

Ask the selector how many recent FIT activities are useful for the next assessment without downloading anything:

```shell
./garmin-workouts activities recommend
```

Automatic selection queries 42 days of running history and normally selects all runs in the latest 28-day window. It backfills older runs until at least six are available and caps unusually dense samples at 16. The JSON response explains the choice and prints the exact suggested download command.

Prepare the recommended assessment bundle:

```shell
./garmin-workouts activities prepare
```

To request an exact number instead:

```shell
./garmin-workouts activities prepare --last 12
```

Garmin's original activity download is normally a ZIP. The tool safely extracts FIT payloads, validates their headers, names them by date and immutable Garmin activity ID, calculates SHA-256 checksums, and writes `manifest.json`. Repeating the command reuses identical files rather than creating duplicates.

The default output is `personal_activities/assessment-DATE/`. This directory is ignored by Git, and the directory, FIT files, and manifest are created with private local permissions. Use `--output` to choose a different private location.

Example chat-assisted workflow after a training block:

```text
Fetch the recommended recent Garmin FIT assessment set, assess my progress,
and prepare the next four-week running plan for review.
```

The manifest is the stable handoff between Garmin acquisition, FIT analysis, plan generation, and delivery. That separation allows the same code to support this CLI now and a macOS/iOS interface later.

## Four-week FIT-driven workflow

The importer deliberately separates training decisions from delivery:

1. Run `activities recommend` or let the assessment workflow choose the recent sample.
2. Run `activities prepare` to download the selected original FIT files and manifest.
3. Review FIT metrics alongside recovery, symptoms, blood pressure, and current goals.
4. Generate a dated four-week YAML block.
5. Preview and validate the block locally.
6. Explicitly apply it to Garmin Connect.
7. Reassess before the next block rather than automatically advancing after one activity.

The example plan demonstrates this workflow, but it is not medical clearance. The athlete remains responsible for symptom, blood-pressure, recovery, and clinician-defined stop rules before starting a scheduled workout.

## Security and limitations

- `.env`, `.venv`, the Garmin token store, personal plans, and downloaded activity bundles are ignored by Git.
- Use a unique Garmin password and protect `.env` with local file permissions.
- Do not run an unattended cloud job containing Garmin credentials.
- Garmin's private endpoints, MFA, CAPTCHA, or SSO changes can break authentication.
- Reapplying a plan is designed to be repeatable, but deleting or moving old calendar entries remains a manual Garmin Connect action.

## Development

```shell
uv sync --dev
mise run check
```

This running-only fork is based on [mkuthan/garmin-workouts](https://github.com/mkuthan/garmin-workouts). This project remains available under the Apache-2.0 license.
