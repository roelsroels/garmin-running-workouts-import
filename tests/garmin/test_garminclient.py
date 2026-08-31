from unittest.mock import MagicMock, mock_open, patch

import pytest

from garminworkouts.garmin.garminclient import (
    GarminAuthenticationError,
    GarminClient,
    GarminMFARequiredError,
    GarminTokenPersistenceError,
)
from garminworkouts.garmin.ratelimit import GarminRateLimitError


def _rate_limiter():
    return MagicMock()


def _client_with_session():
    client = GarminClient("user", "password", ".tokens", rate_limiter=_rate_limiter())
    client.session = MagicMock()
    return client


@patch("garminconnect.Garmin")
def test_context_logs_in_with_token_store(garmin):
    session = garmin.return_value
    limiter = _rate_limiter()
    with GarminClient("user", "password", ".tokens", rate_limiter=limiter) as connection:
        assert connection.session is session
    garmin.assert_called_once_with("user", "password")
    session.login.assert_called_once_with(tokenstore=".tokens")
    assert session.client.skip_strategies == {
        "mobile+cffi",
        "widget+cffi",
        "portal+cffi",
        "portal+requests",
    }
    limiter.before_request.assert_called_once_with()
    limiter.record_success.assert_called_once_with()


@patch("garminconnect.Garmin")
def test_context_records_login_rate_limit_and_does_not_retry(garmin):
    from garminconnect import GarminConnectTooManyRequestsError

    limiter = _rate_limiter()
    limiter.record_rate_limited.return_value = GarminRateLimitError(2_000_000_000)
    garmin.return_value.login.side_effect = GarminConnectTooManyRequestsError("429")
    connection = GarminClient("user", "password", ".tokens", rate_limiter=limiter)

    with pytest.raises(GarminRateLimitError, match="No automatic retry"):
        with connection:
            pass

    garmin.return_value.login.assert_called_once_with(tokenstore=".tokens")
    limiter.record_rate_limited.assert_called_once()
    assert connection.session is None


@patch("garminconnect.Garmin")
def test_context_supplies_mfa_prompt_without_changing_normal_login(garmin):
    prompt = MagicMock(return_value="123456")
    garmin.return_value.login.return_value = (None, None)

    with GarminClient("user", "password", ".tokens", prompt_mfa=prompt):
        pass

    garmin.assert_called_once_with("user", "password", prompt_mfa=prompt)
    garmin.return_value.login.assert_called_once_with(tokenstore=".tokens")
    prompt.assert_not_called()  # The dependency invokes it only when Garmin requests MFA.


@patch("garminconnect.Garmin")
def test_deferred_mfa_clears_password_resumes_and_persists_tokens(garmin, tmp_path):
    session = garmin.return_value
    session.password = "password"
    session.login.return_value = ("needs_mfa", None)
    limiter = _rate_limiter()
    token_store = tmp_path / "tokens"
    connection = GarminClient(
        "user",
        "password",
        token_store,
        rate_limiter=limiter,
        defer_mfa=True,
    )

    assert connection.open() is connection
    session.login.assert_called_once_with(tokenstore=None)
    assert connection.mfa_required
    assert connection.password is None
    assert session.password is None
    limiter.record_success.assert_not_called()

    assert connection.resume_mfa("123456") is connection
    assert not connection.mfa_required
    session.resume_login.assert_called_once_with({}, "123456")
    assert session.client._tokenstore_path == str(token_store.resolve())
    session.client.dump.assert_called_once_with(str(token_store.resolve()))
    assert limiter.before_request.call_count == 2
    limiter.record_success.assert_called_once_with()


@patch("garminconnect.Garmin")
def test_deferred_web_login_bypasses_stale_tokens_and_persists_clean_login(garmin, tmp_path):
    garmin.return_value.login.return_value = (None, None)
    limiter = _rate_limiter()
    token_store = tmp_path / "tokens"

    connection = GarminClient(
        "user",
        "password",
        token_store,
        rate_limiter=limiter,
        defer_mfa=True,
    ).open()

    garmin.return_value.login.assert_called_once_with(tokenstore=None)
    garmin.return_value.client.dump.assert_called_once_with(str(token_store.resolve()))
    assert not connection.mfa_required
    assert connection.password is None
    assert garmin.return_value.password is None
    limiter.record_success.assert_called_once_with()


@patch("garminconnect.Garmin")
def test_deferred_mfa_rejects_bad_code_without_using_garmin(garmin):
    garmin.return_value.login.return_value = ("needs_mfa", None)
    connection = GarminClient("user", "password", ".tokens", rate_limiter=_rate_limiter(), defer_mfa=True).open()

    with pytest.raises(GarminAuthenticationError, match="Enter the verification code"):
        connection.resume_mfa("bad code")

    garmin.return_value.resume_login.assert_not_called()
    assert connection.mfa_required


@patch("garminconnect.Garmin")
def test_mfa_authentication_failure_is_safe_and_retryable(garmin):
    from garminconnect import GarminConnectAuthenticationError

    garmin.return_value.login.return_value = ("needs_mfa", None)
    garmin.return_value.resume_login.side_effect = GarminConnectAuthenticationError("sensitive response")
    connection = GarminClient("user", "password", ".tokens", rate_limiter=_rate_limiter(), defer_mfa=True).open()

    with pytest.raises(GarminAuthenticationError, match="rejected the verification code") as error:
        connection.resume_mfa("123456")

    assert "sensitive" not in str(error.value)
    assert connection.mfa_required


@patch("garminconnect.Garmin")
def test_mfa_token_persistence_failure_is_distinct_from_a_bad_code(garmin):
    garmin.return_value.login.return_value = ("needs_mfa", None)
    garmin.return_value.client.dump.side_effect = OSError("read-only token directory")
    connection = GarminClient("user", "password", ".tokens", rate_limiter=_rate_limiter(), defer_mfa=True).open()

    with pytest.raises(GarminTokenPersistenceError, match="tokens could not be saved"):
        connection.resume_mfa("123456")

    garmin.return_value.resume_login.assert_called_once_with({}, "123456")


@patch("garminconnect.Garmin")
def test_mfa_rate_limit_uses_existing_cooldown(garmin):
    from garminconnect import GarminConnectTooManyRequestsError

    limiter = _rate_limiter()
    limiter.record_rate_limited.return_value = GarminRateLimitError(2_000_000_000)
    garmin.return_value.login.return_value = ("needs_mfa", None)
    garmin.return_value.resume_login.side_effect = GarminConnectTooManyRequestsError("429")
    connection = GarminClient("user", "password", ".tokens", rate_limiter=limiter, defer_mfa=True).open()

    with pytest.raises(GarminRateLimitError, match="No automatic retry"):
        connection.resume_mfa("123456")

    limiter.record_rate_limited.assert_called_once()


@patch("garminconnect.Garmin")
def test_noninteractive_mfa_failure_has_actionable_message(garmin):
    from garminconnect import GarminConnectAuthenticationError

    garmin.return_value.login.side_effect = GarminConnectAuthenticationError(
        "MFA Required but no prompt_mfa mechanism supplied"
    )

    with pytest.raises(GarminMFARequiredError, match="interactive CLI or web login"):
        with GarminClient("user", "password", ".tokens", rate_limiter=_rate_limiter()):
            pass


@patch("garminconnect.Garmin")
def test_interactive_mfa_rejection_is_not_reported_as_bad_password(garmin):
    from garminconnect import GarminConnectAuthenticationError

    garmin.return_value.login.side_effect = GarminConnectAuthenticationError("MFA verification failed")

    with pytest.raises(GarminAuthenticationError, match="rejected the verification code"):
        with GarminClient(
            "user",
            "password",
            ".tokens",
            rate_limiter=_rate_limiter(),
            prompt_mfa=lambda: "123456",
        ):
            pass


def test_list_workouts_paginates():
    client = _client_with_session()
    first = [{"workoutId": 1}, {"workoutId": 2}]
    client.session.get_workouts.side_effect = [first, []]
    assert list(client.list_workouts(batch_size=2)) == first
    assert client.session.get_workouts.call_count == 2


def test_list_workouts_stops_after_short_page():
    client = _client_with_session()
    client.session.get_workouts.return_value = [{"workoutId": 1}]
    assert list(client.list_workouts(batch_size=2)) == [{"workoutId": 1}]
    client.session.get_workouts.assert_called_once_with(start=0, limit=2)


def test_workout_operations_delegate_to_current_api():
    client = _client_with_session()
    payload = {"workoutName": "Run"}

    client.get_workout(7)
    client.save_workout(payload)
    client.update_workout(7, payload)
    client.delete_workout(7)
    client.schedule_workout(7, "2026-08-11")
    client.unschedule_workout(17)
    client.list_scheduled_workouts(2026, 8)

    client.session.get_workout_by_id.assert_called_once_with(7)
    client.session.upload_workout.assert_called_once_with(payload)
    client.session.update_workout.assert_called_once_with(7, payload)
    client.session.delete_workout.assert_called_once_with(7)
    client.session.schedule_workout.assert_called_once_with(7, "2026-08-11")
    client.session.unschedule_workout.assert_called_once_with(17)
    client.session.get_scheduled_workouts.assert_called_once_with(2026, 8)


def test_download_workout_writes_bytes():
    client = _client_with_session()
    client.session.download_workout.return_value = b"fit"
    with patch("builtins.open", mock_open()) as output:
        client.download_workout(7, "run.fit")
    output.assert_called_once_with("run.fit", "wb")
    output().write.assert_called_once_with(b"fit")


def test_list_scheduled_workouts_rejects_invalid_month():
    client = _client_with_session()
    with pytest.raises(ValueError, match="Month must be between"):
        client.list_scheduled_workouts(2026, 13)


def test_activity_operations_delegate_to_current_api():
    client = _client_with_session()
    client.session.ActivityDownloadFormat.ORIGINAL = "original"
    client.session.get_activities.return_value = [{"activityId": 1}]
    client.session.get_activities_by_date.return_value = [{"activityId": 2}]
    client.session.download_activity.return_value = b"original"

    assert client.list_recent_activities(12) == [{"activityId": 1}]
    assert client.list_activities_by_date("2026-07-01", "2026-08-01") == [{"activityId": 2}]
    assert client.download_activity_original(2) == b"original"

    client.session.get_activities.assert_called_once_with(start=0, limit=12, activitytype="running")
    client.session.get_activities_by_date.assert_called_once_with(
        "2026-07-01",
        "2026-08-01",
        activitytype="running",
        sortorder="desc",
    )
    client.session.download_activity.assert_called_once_with(2, dl_fmt="original")
