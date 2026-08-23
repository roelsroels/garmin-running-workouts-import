# Automation architecture and roadmap

The project keeps Garmin access, FIT assessment, training decisions, and user interfaces as separate layers. This avoids coupling evidence-based planning logic to Garmin's private API or to one command-line interface.

## Implemented today

1. **Garmin access** authenticates locally, lists running activities, and downloads original activity exports.
2. **Assessment selection** adapts to training frequency, recommends a recent 6–16-run sample, and supports an explicit last-N override.
3. **Private FIT evidence** extracts and validates downloads, deduplicates them, records checksums in a versioned manifest, and decodes them locally with Garmin's FIT SDK.
4. **Deterministic planning** combines a structured goal, availability, constraints, demonstrated recent frequency, median duration, longest-run exposure, and confidence rules into a reviewable dated proposal.
5. **Supervised adaptation** refreshes completed and missed dates, reassesses recent FIT evidence, and proposes changes only for dates that have not passed.
6. **Progress evidence** stores active-block status in SQLite and shows completed, missed, current-day, and future workouts consistently in the CLI and web interface. Garmin workout execution scores are fetched once when available and cached locally.
7. **Workout delivery** previews conflicts, creates or updates workout definitions, and schedules approved dated running workouts.
8. **Protected retirement and cleanup** previews exact Garmin targets, protects definitions reused by active plans, unschedules obsolete future entries, and can remove old workout templates without touching recorded activities.

The manifest and analysis JSON are stable boundaries between acquisition, interpretation, planning, and delivery. Both the CLI and web interface call the same Python domain services instead of duplicating Garmin or planning logic.

## Further intelligence work

The current FIT analyzer already records activity summaries, laps, record-level samples, data-quality flags, and descriptive pace-to-heart-rate decoupling when sufficient fields exist. Useful next steps are:

- compare planned steps with recorded laps or structured-workout steps instead of relying mainly on date-level adherence;
- derive repeatable interval consistency and recovery summaries with explicit data-quality requirements;
- add longitudinal load and comparable-session views without turning one metric into an automatic progression rule;
- collect optional runner-reported RPE, recovery, pain, illness, weather, and reasons for modifying a workout;
- expose the evidence, rule, confidence, and resulting change for every proposed adaptation; and
- support separately versioned planning strategies that can be evaluated against fixtures before becoming selectable.

Analysis output should continue to record which FIT fields supported each conclusion. Subjective RPE, sleep, terrain, weather, illness, injury, schedule changes, and optional health-professional instructions require explicit user input and must not be inferred from FIT data.

## Plan policy layer

Plan generation takes structured assessment results plus explicit constraints and produces a previewable YAML proposal. It remains conservative and explains its confidence and main construction decisions. Applying or replacing a plan in Garmin continues to require an explicit action.

Useful policy inputs include training age, available days, long-run preference, target event or performance, recent adherence, current tolerable distance, reported effort, injury status, and any user-defined limits. Health constraints are optional inputs supplied by the runner; the planner does not infer diagnoses or medical clearance from activity data.

## Application path

1. Formalize a versioned local API or subprocess protocol around the existing Python services and JSON/YAML artifacts.
2. Add a richer evidence review showing selected activities, data-quality limits, assessment findings, and planned-versus-completed details.
3. Separate profiles, credentials, and mutable state safely before considering a multi-user service.
4. Use platform-secure credential storage in a distributed desktop or mobile application while retaining an architecture-neutral file-token option for servers.
5. Reuse the goal, plan, manifest, and analysis schemas in macOS/iOS clients, with Garmin authentication constraints evaluated separately.

Garmin Connect uses private endpoints that can change without notice. The application should present authentication and API failures clearly, retain local data, and never silently alter a training calendar.
