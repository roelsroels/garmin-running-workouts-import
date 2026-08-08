# Automation architecture and roadmap

The project keeps Garmin access, FIT assessment, training decisions, and user interfaces as separate layers. This avoids coupling evidence-based planning logic to Garmin's private API or to one command-line interface.

## Implemented foundation

1. **Garmin access** authenticates locally, lists running activities, and downloads original activity exports.
2. **Assessment selection** adapts to training frequency, recommends a recent sample, and supports an explicit last-N override.
3. **Private archive** extracts and validates FIT files, deduplicates them, and produces a versioned manifest with checksums.
4. **Workout delivery** previews, creates or updates, and schedules dated running workouts.
5. **Plan retirement** previews exact Garmin targets, protects definitions reused by active plans, unschedules future entries, and removes old workout templates without touching recorded activities.

The manifest is the boundary between acquisition and analysis. A future UI should consume the manifest rather than call CLI parsing code.

## Next intelligence layer

A FIT analysis service can add derived, reproducible metrics to a second versioned artifact:

- activity and moving time, distance, pace, heart-rate distribution, and elevation;
- interval rep execution and recovery between efforts;
- pace-to-heart-rate relationship and within-run aerobic decoupling;
- cadence, ground-contact time, vertical oscillation, and left/right balance when recorded;
- long-run continuity, stopped time, and week-over-week load;
- data-quality flags for missing sensors, GPS problems, or implausible values.

The analysis result should record which FIT fields supported each conclusion. Subjective RPE, sleep, terrain, weather, illness, injury, schedule changes, and optional health-professional instructions require explicit user input and must not be inferred from FIT data.

## Plan policy layer

Plan generation should take structured assessment results plus explicit constraints and produce a previewable YAML proposal. It should remain conservative and explain every progression or regression. Applying the plan to Garmin must continue to require an explicit action.

Useful policy inputs include training age, available days, long-run preference, target event or performance, recent adherence, current tolerable distance, reported effort, injury status, and any user-defined limits. Health constraints are optional inputs supplied by the runner; the planner does not infer diagnoses or medical clearance from activity data.

## Application path

1. Keep the Python services as the local engine and expose structured JSON inputs/outputs.
2. Add a small local API or subprocess protocol for a macOS application.
3. Use platform-secure credential storage rather than `.env` in a distributed app.
4. Add a review screen for selected activities, assessment findings, and the proposed calendar before upload.
5. Reuse the plan and manifest schemas in an iOS client, with Garmin authentication constraints evaluated separately.

Garmin Connect uses private endpoints that can change without notice. The application should present authentication and API failures clearly, retain local data, and never silently alter a training calendar.
