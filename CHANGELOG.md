# Changelog

All notable changes to Garmin Running Workouts Import are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The web footer now shows the running application version and Git branch for quick deployment identification.
- The full calendar now offers an offline .ics download of upcoming scheduled runs as all-day events for Apple Calendar
  and other calendar apps, including workout instructions and stable event IDs for repeated exports of the same plan.

## [1.2.1] - 2026-08-24

Slow Garmin operations now provide a clear, accessible waiting state instead of leaving the submitted page looking
unresponsive.

### Added

- Added an accessible running-puppet wait screen for Garmin-backed actions so slow schedule, refresh, inspection, and
  connection requests clearly remain in progress.

## [1.2.0] - 2026-08-23

Replanning now uses completed training as immutable evidence while limiting proposals and Garmin changes to the
remaining actionable schedule.

### Changed

- Replanning now treats completed workouts as immutable planning evidence, limits proposals and Garmin changes to
  upcoming scheduled workouts, and leaves missed past workouts in history instead of moving them automatically.
- Duration-based easy workouts are normalized to the nearest five minutes, and normalized comparisons suppress
  rounding-only calendar changes.
- Active-plan generation now reassesses only the remaining mutable schedule, using completed runs as evidence while
  keeping them in a separate completed/missed history on proposal reviews.

### Fixed

- Replacement proposals that still contain past, completed, or missed workouts are rejected before Garmin is read or
  changed, and final-week long runs retain their intended lower-load reduction.

## [1.1.0] - 2026-08-23

Calendar feedback and completed-workout evidence are now more informative while remaining conservative with Garmin requests.

### Added

- Completed planned runs now show Garmin's workout execution score in the web and CLI calendars when the FIT activity provides one.
- Execution scores are fetched once per newly completed planned run and cached locally to avoid repeated Garmin requests.
- Existing SQLite databases migrate automatically to store execution-score values and checked state.

### Changed

- Elapsed scheduled workouts without a matched running activity are shown as missed in the web calendar, including a provisional **Missed · needs refresh** state before Garmin progress is refreshed.
- Refreshed the anonymized web screenshots to show completed scores, missed dates, today's next workout, and future scheduled runs.
- Updated the README and automation roadmap to distinguish implemented FIT analysis and adaptation features from future work.

## [1.0.0] - 2026-08-20

First stable release of the running-only Garmin planning and scheduling application.

### Added

- An anonymized web-interface screenshot gallery in the README.
- GitHub sponsorship metadata for the project support link.

### Changed

- The calendar now subdues completed, missed, skipped, and past workout dates.
- The first scheduled workout on the current date or later is highlighted as **Next**.
- A workout scheduled for today has a prominent **Today · scheduled** status.
- Past workouts whose Garmin progress has not yet been refreshed are clearly marked **Past / refresh** rather than presented as the next action.

## [0.9.0] - 2026-08-10

This is the first tagged release of the running-only fork.

### Added

- A goal-driven interactive planner that creates and manages training blocks without requiring YAML editing.
- Portable SQLite state for goals, plan history, progress, settings, and an audit trail.
- Garmin activity discovery, adaptive FIT-file selection, private original-file download, and local FIT decoding.
- Supervised mid-block assessment and replacement of future workouts after explicit approval.
- User-supplied heart-rate caps, ranges, or Garmin zones by workout phase, plus a one-off HR workout builder.
- A responsive web interface for goals, calendar review, plan previews, HR workouts, cleanup, and settings.
- Garmin-side overlap detection and preview-first retirement of obsolete scheduled workouts and templates.
- Optional provider-neutral LLM explanations; the deterministic planner remains fully functional without an LLM.
- Generic systemd and nginx deployment examples, a guarded server publishing helper, and web branding assets.

### Changed

- Refocused the upstream project on running workouts and removed cycling-specific content.
- Updated workout creation and scheduling for the current `garminconnect` client API.
- Made planning rules deterministic, conservative, locally auditable, and explicit about their evidence boundary.
- Standardized local application data under `~/.garmin-running-workouts/` by default.

### Fixed

- Added a loader-independent repository launcher for hardened Python environments.
- Prevented unrelated duplicate Garmin workout names from blocking plan application.
- Improved workout-ID resolution, schedule conflict handling, and duplicate-date cleanup.
- Added authentication cooldowns and request pacing to reduce avoidable Garmin rate-limit responses.

### Security

- Garmin passwords and optional LLM API keys are requested at runtime and are not stored by the application.
- Reusable Garmin session tokens and the SQLite database are stored with restrictive local permissions.
- The web interface includes CSRF protection, a restrictive content-security policy, and security headers.
- Production deployment guidance keeps the application on a private loopback listener behind an authenticated TLS proxy.
