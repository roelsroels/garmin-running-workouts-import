import pytest

from garminworkouts.garmin.provider import (
    CURRENT_ACTIVITY_REFRESH_CAPABILITIES,
    CURRENT_PLAN_APPLY_CAPABILITIES,
    CURRENT_PLAN_RETIRE_CAPABILITIES,
    LEGACY_WEB_PROVIDER,
    OFFICIAL_ENTERPRISE_PROVIDER,
    OFFICIAL_PERSONAL_PROVIDER,
    OFFICIAL_STARTER_PROVIDER,
    GarminCapability,
    GarminCapabilityError,
    require_capabilities,
    require_connection_capabilities,
)


def test_existing_web_provider_retains_every_current_workflow_capability():
    assert LEGACY_WEB_PROVIDER.supports(*CURRENT_ACTIVITY_REFRESH_CAPABILITIES)
    assert LEGACY_WEB_PROVIDER.supports(*CURRENT_PLAN_APPLY_CAPABILITIES)
    assert LEGACY_WEB_PROVIDER.supports(*CURRENT_PLAN_RETIRE_CAPABILITIES)


def test_official_personal_profile_can_import_but_not_publish_training():
    assert OFFICIAL_PERSONAL_PROVIDER.supports(*CURRENT_ACTIVITY_REFRESH_CAPABILITIES)
    assert not OFFICIAL_PERSONAL_PROVIDER.supports(GarminCapability.TRAINING_PLAN_PUBLISH)
    assert not OFFICIAL_PERSONAL_PROVIDER.supports(*CURRENT_PLAN_APPLY_CAPABILITIES)


def test_official_business_profiles_support_training_crud_without_library_listing():
    assert OFFICIAL_STARTER_PROVIDER.supports(GarminCapability.TRAINING_PLAN_PUBLISH)
    assert OFFICIAL_ENTERPRISE_PROVIDER.supports(GarminCapability.TRAINING_PLAN_PUBLISH)
    assert OFFICIAL_STARTER_PROVIDER.supports(
        GarminCapability.WORKOUT_CREATE,
        GarminCapability.WORKOUT_UPDATE,
        GarminCapability.WORKOUT_DELETE,
        GarminCapability.CALENDAR_READ,
        GarminCapability.CALENDAR_SCHEDULE,
        GarminCapability.CALENDAR_UNSCHEDULE,
    )
    assert not OFFICIAL_STARTER_PROVIDER.supports(GarminCapability.WORKOUT_LIBRARY_READ)
    assert not OFFICIAL_STARTER_PROVIDER.supports(*CURRENT_PLAN_APPLY_CAPABILITIES)
    assert not OFFICIAL_ENTERPRISE_PROVIDER.supports(*CURRENT_PLAN_RETIRE_CAPABILITIES)


def test_enterprise_adds_pull_and_historical_activity_access():
    assert not OFFICIAL_STARTER_PROVIDER.supports(GarminCapability.ACTIVITY_PULL)
    assert not OFFICIAL_STARTER_PROVIDER.supports(GarminCapability.ACTIVITY_HISTORICAL_BACKFILL)
    assert OFFICIAL_ENTERPRISE_PROVIDER.supports(
        GarminCapability.ACTIVITY_PULL,
        GarminCapability.ACTIVITY_HISTORICAL_BACKFILL,
    )


def test_capability_guard_explains_unsupported_operation():
    with pytest.raises(GarminCapabilityError, match="missing capabilities: training-plan-publish"):
        require_capabilities(
            OFFICIAL_PERSONAL_PROVIDER,
            "publish a training plan",
            GarminCapability.TRAINING_PLAN_PUBLISH,
        )


def test_connection_guard_checks_declared_profiles_but_allows_simple_test_doubles():
    class OfficialPersonalConnection:
        provider_profile = OFFICIAL_PERSONAL_PROVIDER

    with pytest.raises(GarminCapabilityError, match="training-plan-publish"):
        require_connection_capabilities(
            OfficialPersonalConnection(),
            "publish a training plan",
            GarminCapability.TRAINING_PLAN_PUBLISH,
        )

    require_connection_capabilities(object(), "exercise a test double", GarminCapability.WORKOUT_CREATE)
