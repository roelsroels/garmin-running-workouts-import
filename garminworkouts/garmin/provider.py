"""Garmin connection capabilities independent of a specific API implementation.

Garmin's official Developer Program and the authenticated Garmin Connect web
client expose different operation sets.  Keeping those sets explicit prevents
future providers from silently claiming support for workflows they cannot
complete safely.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class GarminCapability(StrEnum):
    ACTIVITY_PULL = "activity-pull"
    ACTIVITY_PUSH = "activity-push"
    ACTIVITY_FIT_DOWNLOAD = "activity-fit-download"
    ACTIVITY_HISTORICAL_BACKFILL = "activity-historical-backfill"
    WORKOUT_LIBRARY_READ = "workout-library-read"
    WORKOUT_CREATE = "workout-create"
    WORKOUT_UPDATE = "workout-update"
    WORKOUT_DELETE = "workout-delete"
    CALENDAR_READ = "calendar-read"
    CALENDAR_SCHEDULE = "calendar-schedule"
    CALENDAR_UNSCHEDULE = "calendar-unschedule"
    TRAINING_PLAN_PUBLISH = "training-plan-publish"


@dataclass(frozen=True)
class GarminProviderProfile:
    key: str
    name: str
    official: bool
    capabilities: frozenset[GarminCapability]

    def supports(self, *capabilities: GarminCapability) -> bool:
        return set(capabilities).issubset(self.capabilities)

    def missing(self, *capabilities: GarminCapability) -> tuple[GarminCapability, ...]:
        return tuple(capability for capability in capabilities if capability not in self.capabilities)


class GarminCapabilityError(RuntimeError):
    """Raised before a provider is asked to perform an unsupported workflow."""


def require_capabilities(
    provider: GarminProviderProfile,
    operation: str,
    *capabilities: GarminCapability,
) -> None:
    missing = provider.missing(*capabilities)
    if not missing:
        return
    missing_text = ", ".join(capability.value for capability in missing)
    raise GarminCapabilityError(f"{provider.name} cannot {operation}; missing capabilities: {missing_text}")


def require_connection_capabilities(connection, operation: str, *capabilities: GarminCapability) -> None:
    """Guard real providers while remaining friendly to small test doubles.

    Production providers must expose a ``GarminProviderProfile``. Existing
    duck-typed test connections predate this contract and are intentionally
    left alone until they are replaced by shared provider fixtures.
    """
    provider = getattr(connection, "provider_profile", None)
    if isinstance(provider, GarminProviderProfile):
        require_capabilities(provider, operation, *capabilities)


@runtime_checkable
class GarminProvider(Protocol):
    """Minimum identity contract shared by current and future providers."""

    provider_profile: GarminProviderProfile


LEGACY_WEB_PROVIDER = GarminProviderProfile(
    key="garmin-connect-web",
    name="Authenticated Garmin Connect web client",
    official=False,
    capabilities=frozenset(
        {
            GarminCapability.ACTIVITY_PULL,
            GarminCapability.ACTIVITY_FIT_DOWNLOAD,
            GarminCapability.ACTIVITY_HISTORICAL_BACKFILL,
            GarminCapability.WORKOUT_LIBRARY_READ,
            GarminCapability.WORKOUT_CREATE,
            GarminCapability.WORKOUT_UPDATE,
            GarminCapability.WORKOUT_DELETE,
            GarminCapability.CALENDAR_READ,
            GarminCapability.CALENDAR_SCHEDULE,
            GarminCapability.CALENDAR_UNSCHEDULE,
        }
    ),
)

# Public Garmin documentation, checked 2026-10-21, describes only these
# capabilities for each official tier.  Do not add read/update/delete semantics
# to the Training API profiles until Garmin's gated reference confirms them.
OFFICIAL_PERSONAL_PROVIDER = GarminProviderProfile(
    key="garmin-connect-official-personal",
    name="Official Garmin Connect Personal API",
    official=True,
    capabilities=frozenset(
        {
            GarminCapability.ACTIVITY_PULL,
            GarminCapability.ACTIVITY_FIT_DOWNLOAD,
        }
    ),
)

OFFICIAL_STARTER_PROVIDER = GarminProviderProfile(
    key="garmin-connect-official-starter",
    name="Official Garmin Connect Business Starter API",
    official=True,
    capabilities=frozenset(
        {
            GarminCapability.ACTIVITY_PUSH,
            GarminCapability.ACTIVITY_FIT_DOWNLOAD,
            GarminCapability.TRAINING_PLAN_PUBLISH,
        }
    ),
)

OFFICIAL_ENTERPRISE_PROVIDER = GarminProviderProfile(
    key="garmin-connect-official-enterprise",
    name="Official Garmin Connect Business Enterprise API",
    official=True,
    capabilities=frozenset(
        {
            GarminCapability.ACTIVITY_PULL,
            GarminCapability.ACTIVITY_PUSH,
            GarminCapability.ACTIVITY_FIT_DOWNLOAD,
            GarminCapability.ACTIVITY_HISTORICAL_BACKFILL,
            GarminCapability.TRAINING_PLAN_PUBLISH,
        }
    ),
)


# The present implementation depends on granular workout-library and calendar
# CRUD.  The official Training API is expected to use a different plan-publish
# workflow rather than pretending to satisfy this contract.
CURRENT_PLAN_APPLY_CAPABILITIES = (
    GarminCapability.WORKOUT_LIBRARY_READ,
    GarminCapability.WORKOUT_CREATE,
    GarminCapability.WORKOUT_UPDATE,
    GarminCapability.CALENDAR_READ,
    GarminCapability.CALENDAR_SCHEDULE,
)

CURRENT_PLAN_RETIRE_CAPABILITIES = (
    GarminCapability.WORKOUT_LIBRARY_READ,
    GarminCapability.WORKOUT_DELETE,
    GarminCapability.CALENDAR_READ,
    GarminCapability.CALENDAR_UNSCHEDULE,
)

CURRENT_PLAN_RETIRE_PREVIEW_CAPABILITIES = (
    GarminCapability.WORKOUT_LIBRARY_READ,
    GarminCapability.CALENDAR_READ,
)

CURRENT_ACTIVITY_REFRESH_CAPABILITIES = (
    GarminCapability.ACTIVITY_PULL,
    GarminCapability.ACTIVITY_FIT_DOWNLOAD,
)
