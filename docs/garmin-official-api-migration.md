# Garmin's 2026 Connect Developer Program: project impact and migration plan

Assessment date: **21 October 2026**

Garmin has reopened and reorganized the Garmin Connect Developer Program. This is good news for this project: there is
now a supported route for both activity ingestion and structured-workout publishing. It is **not** an immediate breaking
change to the authenticated Garmin Connect web client currently used by the project, and the public official API does
not yet provide a drop-in replacement for every operation the application performs.

Sources:

- [DCRainmaker's announcement and tier overview](https://www.dcrainmaker.com/2026/10/garmin-connect-api-revamps-connect-developer.html)
- [Garmin's current Personal, Starter and Enterprise overview](https://developer.garmin.com/gc-developer-program/overview/)
- [Garmin Personal API FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/)
- [Garmin Personal API Swagger reference](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/)
- [Garmin Connect Developer Program](https://developer.garmin.com/gc-developer-program/)
- [Garmin Activity API](https://developer.garmin.com/gc-developer-program/activity-api/)
- [Garmin Training API](https://developer.garmin.com/gc-developer-program/training-api/)
- [Garmin API brand guidelines](https://developer.garmin.com/downloads/brand/Garmin-Developer-API-Brand-Guidelines.pdf)

## Executive conclusion

- **No immediate outage is implied.** The current `garminconnect`-based provider still works and remains the default.
- **Personal access can replace the read side for one runner.** It provides pull access to that person's activity JSON
  and FIT data, which covers activity assessment and most FIT analysis without a Garmin password or private web login.
- **Personal access cannot upload the training schedule.** Garmin lists the Training API only for Business Starter and
  Enterprise access.
- **Business Starter is the likely target for this open-source application.** It includes the Training API and activity
  JSON/FIT for a limited user population, but activity delivery is push-only and historical backfill is not included.
- **Enterprise is only required by documented scale or history needs.** Garmin lists push or ping/pull delivery, at
  least one month of historical data, unlimited users and support at that tier.
- **A full migration cannot be implemented responsibly from the public marketing pages alone.** The request/response
  schemas, OAuth application credentials and precise Training API lifecycle semantics are available after Garmin grants
  program access.

## Capability mapping

| Project behavior | Current web provider | Official Personal | Business Starter | Business Enterprise |
| --- | --- | --- | --- | --- |
| Pull recent activity summaries | Yes | Yes | No; push delivery | Yes; push or ping/pull |
| Download activity FIT | Yes | Yes | Yes, for delivered activities | Yes |
| Historical first-run assessment | Yes | Not explicitly constrained publicly | No | At least one month |
| Publish workouts/training plans | Yes, via private workout endpoints | No | Yes, Training API | Yes, Training API |
| Read/update/delete workout-library templates | Yes | No | Not stated publicly | Not stated publicly |
| Read/unschedule individual calendar entries | Yes | No | Not stated publicly | Not stated publicly |
| Username/password/MFA login | Yes | Replaced by API key/access token | Replaced by OAuth 2.0 consent | Replaced by OAuth 2.0 consent |

“Not stated publicly” is intentionally different from “not supported.” The application must not promise those operations
until they are confirmed in Garmin's gated API reference and verified in its evaluation environment.

## Architectural consequences

### 1. Split activity ingestion from training publication

The current `GarminClient` performs every Garmin operation behind one authenticated web session. Official access needs
two independently selectable integrations:

1. an **activity source** (Personal pull, Business push/ping-pull, or the existing web provider); and
2. a **training publisher** (official Business Training API or the existing web provider).

This also permits a useful transition mode: official Personal activity/FIT reads plus the existing workout publisher,
until an approved Business application is available.

### 2. Add an asynchronous Business activity inbox

Starter activity delivery is push-only. The web service therefore needs a signed webhook endpoint, durable event
storage, idempotency keys and a processing queue or retryable inbox. `fetch_recent_history()` cannot simply call a list
endpoint under that tier. The CLI can consume activities already stored by the web service, but cannot receive pushes
while it is offline.

### 3. Implement Training API publication as a separate plan strategy

The current `PlanApplier` creates or updates workout-library templates, resolves their IDs, then schedules each one.
Garmin describes the official API as publishing workouts and training plans to the calendar. The official implementation
should translate the project's neutral plan model directly to the Training API instead of emulating the current private
CRUD sequence.

### 4. Keep cleanup conservative

Replacement and retirement currently depend on listing templates and calendar entries, then deleting or unscheduling
specific Garmin IDs after user approval. Until the official API confirms equivalent lifecycle operations, an official
publisher must track every remote ID it creates locally and must not claim that it can discover or remove unrelated
Garmin content. If delete/replace is unavailable, the UI must say so before applying a revision.

### 5. Replace stored Garmin passwords with scoped credentials

The official provider should use Garmin's access-token/OAuth flow, store refresh/access tokens separately from legacy
session tokens, redact all credentials from logs, and keep the legacy password/MFA path only for the legacy provider.
Provider choice and credential migration must be explicit; merely configuring an API token must never invalidate a
working legacy login.

### 6. Revalidate data and presentation assumptions

Before switching defaults, verify:

- heart-rate caps/ranges/zones, pace ranges, repeats, warm-ups and cooldowns map losslessly to Training API steps;
- returned workout/training-plan IDs support idempotent updates and retirement;
- FIT files contain the fields needed for the current execution-score parser (Garmin may omit proprietary FIT fields);
- activity timestamps, sport subtypes, privacy controls and pagination preserve current matching behavior;
- rate-limit responses and retry headers are respected; and
- official Garmin attribution is added wherever required by the brand guidelines.

## Implementation now present in the code

`garminworkouts.garmin.provider` now records provider capabilities without altering the working login flow. The current
web client declares the granular operations it supports. Official Personal, Starter and Enterprise profiles declare
only behavior Garmin documents publicly, and tests prevent the application from accidentally treating Training API
publication as equivalent to workout-library/calendar CRUD.

`garminworkouts.garmin.personalclient` implements Garmin's published Personal API contract for activity search and
original activity downloads. It exchanges the Personal key for a 24-hour OAuth2 access token, caches only that
short-lived token in a private file, paginates activity searches, downloads original ZIP/FIT payloads, applies the
existing conservative rate limiter, and sanitizes authentication and transport errors. Web and interactive CLI
settings can select and test this read source; explicit `activities` commands honor the same environment setting.

The Personal key itself stays in `GARMIN_PERSONAL_API_KEY` and is never stored in SQLite. Provider selection never
changes the separate legacy client used to publish workouts and calendar entries.

## Implementation sequence after access is granted

1. Use the implemented Personal provider in shadow comparisons against the legacy activity/FIT reader.
2. When Business Starter access is granted, capture sanitized contract fixtures for OAuth, activity delivery, Training
   API publication, and any documented
   update/delete callbacks.
3. Implement OAuth 2.0 consent and the durable Starter activity inbox for multi-user installations.
4. Implement an official Training API plan publisher against the neutral `TrainingPlan` model, including HR and pace
   target conformance tests.
5. Add Starter provider selection and connection status while keeping the verified legacy publisher as a fallback.
6. Run a shadow comparison: generate both payloads, publish only through the selected provider, and compare calendar,
   watch display and completed FIT evidence.
7. Promote official publishing only after create, revise, retire, rate-limit and token-refresh tests pass. Document any
   cleanup limitations prominently.

## Information still needed from Garmin

- Personal API quotas and any unpublished burst limits.
- Starter user limit and push delivery retention/retry guarantees.
- Training API schema and supported running-step target types.
- Whether published plans/workouts can be listed, updated, cancelled and deleted, and which IDs are returned.
- Whether Starter permits a controlled initial history import or only activities recorded after consent.
- Whether original FIT downloads retain workout execution score and every field used by assessment.

These are access-program questions, not safe implementation guesses. Once the project has Starter credentials and the
gated documentation, the provider boundary above is ready for the real adapters.
