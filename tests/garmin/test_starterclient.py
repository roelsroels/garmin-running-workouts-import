import base64
import hashlib
import json
import urllib.error
import urllib.parse
from email.message import Message
from unittest.mock import MagicMock

import pytest

from garminworkouts.garmin.provider import OFFICIAL_STARTER_PROVIDER
from garminworkouts.garmin.ratelimit import GarminRateLimitError
from garminworkouts.garmin.starterclient import (
    API_BASE_URL,
    AUTHORIZATION_URL,
    PERMISSIONS_PATH,
    SCHEDULE_PATH,
    TOKEN_URL,
    USER_ID_PATH,
    WORKOUT_CREATE_PATH,
    GarminStarterAuthenticationError,
    GarminStarterNotFoundError,
    GarminStarterPermissionError,
    StarterGarminClient,
)


class Response:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


def _client(tmp_path, responses, clock=lambda: 1_000_000):
    opener = MagicMock()
    opener.open.side_effect = [Response(payload) for payload in responses]
    limiter = MagicMock()
    client = StarterGarminClient(
        "starter-client",
        "starter-secret",
        "https://planner.example.test/settings/garmin/starter/callback",
        tmp_path / "starter-api",
        opener=opener,
        rate_limiter=limiter,
        clock=clock,
    )
    return client, opener, limiter


def _token_response(access="access-one", refresh="refresh-one"):
    return json.dumps(
        {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 86400,
            "refresh_token_expires_in": 7_775_998,
            "scope": "PARTNER_WRITE PARTNER_READ CONNECT_READ CONNECT_WRITE",
        }
    ).encode()


def test_starter_authorization_uses_pkce_state_and_registered_redirect(tmp_path):
    client, _opener, _limiter = _client(tmp_path, [])

    authorization = client.authorization()
    parsed = urllib.parse.urlsplit(authorization.url)
    query = urllib.parse.parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZATION_URL
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["starter-client"]
    assert query["redirect_uri"] == ["https://planner.example.test/settings/garmin/starter/callback"]
    assert query["state"] == [authorization.state]
    assert query["code_challenge_method"] == ["S256"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(authorization.code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode()
    )
    assert query["code_challenge"] == [expected]
    assert 43 <= len(authorization.code_verifier) <= 128


def test_starter_exchanges_code_stores_rotating_tokens_and_checks_identity(tmp_path):
    client, opener, limiter = _client(
        tmp_path,
        [
            _token_response(),
            b'{"userId":"runner-id"}',
            b'{"permissions":["WORKOUT_IMPORT","ACTIVITY_EXPORT"]}',
        ],
    )

    client.exchange_code("authorization-code", "verifier-value")
    status = client.connection_status()

    token_request = opener.open.call_args_list[0].args[0]
    token_body = urllib.parse.parse_qs(token_request.data.decode())
    assert token_request.full_url == TOKEN_URL
    assert (
        token_request.get_header("Authorization")
        == "Basic " + base64.b64encode(b"starter-client:starter-secret").decode()
    )
    assert token_body == {
        "grant_type": ["authorization_code"],
        "code": ["authorization-code"],
        "code_verifier": ["verifier-value"],
        "redirect_uri": ["https://planner.example.test/settings/garmin/starter/callback"],
    }
    assert status == {"user_id": "runner-id", "permissions": ("WORKOUT_IMPORT", "ACTIVITY_EXPORT")}
    assert opener.open.call_args_list[1].args[0].full_url == f"{API_BASE_URL}{USER_ID_PATH}"
    assert opener.open.call_args_list[2].args[0].full_url == f"{API_BASE_URL}{PERMISSIONS_PATH}"
    assert client.provider_profile is OFFICIAL_STARTER_PROVIDER
    assert client.token_path.stat().st_mode & 0o777 == 0o600
    token_text = client.token_path.read_text()
    assert "starter-secret" not in token_text
    assert "authorization-code" not in token_text
    assert limiter.before_request.call_count == 3


def test_starter_refreshes_expiring_token_and_rotates_refresh_token(tmp_path):
    client, opener, _limiter = _client(tmp_path, [_token_response("access-two", "refresh-two"), b"[]"])
    client._save_tokens(
        {
            "version": 1,
            "access_token": "expired-access",
            "refresh_token": "refresh-one",
            "expires_at": 1_000_100,
            "refresh_expires_at": 2_000_000,
            "scope": "",
            "client_fingerprint": client._client_fingerprint,
        }
    )

    assert client.list_scheduled_workouts("2026-10-01", "2026-10-31") == []

    refresh = opener.open.call_args_list[0].args[0]
    assert urllib.parse.parse_qs(refresh.data.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-one"],
    }
    api_request = opener.open.call_args_list[1].args[0]
    assert api_request.get_header("Authorization") == "Bearer access-two"
    assert "refresh-two" in client.token_path.read_text()
    assert "refresh-one" not in client.token_path.read_text()


def test_starter_creates_workout_and_schedule_with_bearer_token(tmp_path):
    client, opener, _limiter = _client(
        tmp_path,
        [_token_response(), b'{"workoutId":42}', b'{"scheduleId":84,"workoutId":42,"date":"2026-10-25"}'],
    )
    client.exchange_code("code", "verifier")

    workout = client.save_workout({"workoutName": "Easy run"})
    schedule = client.schedule_workout(workout["workoutId"], "2026-10-25")

    workout_request = opener.open.call_args_list[1].args[0]
    assert workout_request.full_url == f"{API_BASE_URL}{WORKOUT_CREATE_PATH}"
    assert workout_request.method == "POST"
    assert json.loads(workout_request.data) == {"workoutName": "Easy run"}
    schedule_request = opener.open.call_args_list[2].args[0]
    assert schedule_request.full_url == f"{API_BASE_URL}{SCHEDULE_PATH}"
    assert json.loads(schedule_request.data) == {"workoutId": 42, "date": "2026-10-25"}
    assert schedule["scheduleId"] == 84


def test_starter_maps_permission_and_rate_limit_failures_without_leaking_secrets(tmp_path):
    client, opener, limiter = _client(tmp_path, [])
    client._tokens = {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_at": 2_000_000,
        "refresh_expires_at": 3_000_000,
    }
    opener.open.side_effect = urllib.error.HTTPError(
        f"{API_BASE_URL}{WORKOUT_CREATE_PATH}", 412, "permission denied with secret", {}, None
    )
    with pytest.raises(GarminStarterPermissionError) as error:
        client.save_workout({"workoutName": "Easy"})
    assert "starter-secret" not in str(error.value)
    assert "permission denied" not in str(error.value)

    limited = GarminRateLimitError(2_000_000_000)
    limiter.record_rate_limited.return_value = limited
    opener.open.side_effect = urllib.error.HTTPError(f"{API_BASE_URL}{WORKOUT_CREATE_PATH}", 429, "limited", {}, None)
    with pytest.raises(GarminRateLimitError):
        client.save_workout({"workoutName": "Easy"})
    limiter.record_rate_limited.assert_called_once()


def test_starter_maps_rejected_oauth_code_to_safe_authentication_error(tmp_path):
    client, opener, _limiter = _client(tmp_path, [])
    opener.open.side_effect = urllib.error.HTTPError(TOKEN_URL, 400, "bad code with secret", {}, None)

    with pytest.raises(GarminStarterAuthenticationError, match="rejected or expired") as error:
        client.exchange_code("expired-code", "verifier")

    assert "starter-secret" not in str(error.value)
    assert "bad code" not in str(error.value)


def test_starter_maps_missing_remote_object_to_specific_error(tmp_path):
    client, opener, _limiter = _client(tmp_path, [])
    client._tokens = {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "expires_at": 2_000_000,
        "refresh_expires_at": 3_000_000,
    }
    opener.open.side_effect = urllib.error.HTTPError(
        f"{API_BASE_URL}/training-api/workout/v2/42", 404, "gone", {}, None
    )

    with pytest.raises(GarminStarterNotFoundError):
        client.get_workout(42)
