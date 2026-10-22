import json
import urllib.error
from email.message import Message
from unittest.mock import MagicMock

import pytest

from garminworkouts.garmin.personalclient import (
    ACTIVITY_DOWNLOAD_PATH,
    ACTIVITY_SEARCH_PATH,
    BASE_URL,
    TOKEN_PATH,
    GarminPersonalAuthenticationError,
    PersonalGarminClient,
)
from garminworkouts.garmin.provider import OFFICIAL_PERSONAL_PROVIDER
from garminworkouts.garmin.ratelimit import GarminRateLimitError


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
    client = PersonalGarminClient(
        "personal-secret-key",
        tmp_path / "personal-api",
        opener=opener,
        rate_limiter=limiter,
        clock=clock,
    )
    return client, opener, limiter


def test_personal_client_mints_token_lists_activities_and_uses_bearer_header(tmp_path):
    activities = [{"activityId": 42, "activityName": "Morning Run"}]
    client, opener, limiter = _client(
        tmp_path,
        [json.dumps({"access_token": "official-token", "expires_in": 3600}).encode(), json.dumps(activities).encode()],
    )

    with client as connection:
        assert connection.provider_profile is OFFICIAL_PERSONAL_PROVIDER
        assert connection.list_recent_activities(1) == activities

    token_request = opener.open.call_args_list[0].args[0]
    activity_request = opener.open.call_args_list[1].args[0]
    assert token_request.full_url == f"{BASE_URL}{TOKEN_PATH}"
    assert token_request.method == "POST"
    assert token_request.get_header("Accept") == "application/json"
    assert token_request.get_header("Authorization") == "ApiKey personal-secret-key"
    assert activity_request.full_url.startswith(f"{BASE_URL}{ACTIVITY_SEARCH_PATH}?")
    assert "activityType=running" in activity_request.full_url
    assert activity_request.get_header("Accept") == "application/json"
    assert activity_request.get_header("Authorization") == "Bearer official-token"
    assert limiter.before_request.call_count == 2
    assert limiter.record_success.call_count == 2
    assert client.token_path.stat().st_mode & 0o777 == 0o600
    assert "personal-secret-key" not in client.token_path.read_text()


def test_personal_client_reuses_private_cached_token(tmp_path):
    first, _opener, _limiter = _client(
        tmp_path,
        [json.dumps({"access_token": "cached-token", "expires_in": 3600}).encode()],
    )
    first.open()
    first.close()

    second, opener, _limiter = _client(tmp_path, [b"[]"])
    with second as connection:
        assert connection.list_recent_activities(1) == []

    assert opener.open.call_count == 1
    assert opener.open.call_args.args[0].get_header("Authorization") == "Bearer cached-token"


def test_personal_client_paginates_date_search_and_downloads_original_zip(tmp_path):
    first_page = [{"activityId": index} for index in range(99)]
    final_page = [{"activityId": 100}]
    client, opener, _limiter = _client(
        tmp_path,
        [
            b'{"access_token":"token"}',
            json.dumps(first_page).encode(),
            json.dumps(final_page).encode(),
            b"PK\x03\x04fit-zip",
        ],
    )

    with client as connection:
        activities = connection.list_activities_by_date("2026-09-01", "2026-10-21")
        payload = connection.download_activity_original(100)

    assert len(activities) == 100
    assert payload == b"PK\x03\x04fit-zip"
    first_url = opener.open.call_args_list[1].args[0].full_url
    second_url = opener.open.call_args_list[2].args[0].full_url
    assert "startDate=2026-09-01" in first_url
    assert "endDate=2026-10-21" in first_url
    assert "start=99" in second_url
    assert opener.open.call_args_list[3].args[0].full_url == (
        f"{BASE_URL}{ACTIVITY_DOWNLOAD_PATH.format(activity_id='100')}"
    )
    assert opener.open.call_args_list[3].args[0].get_header("Accept") == "application/x-zip-compressed"


def test_personal_client_sanitizes_authentication_failure(tmp_path):
    client, opener, _limiter = _client(tmp_path, [])
    opener.open.side_effect = urllib.error.HTTPError(f"{BASE_URL}{TOKEN_PATH}", 401, "secret key rejected", {}, None)

    with pytest.raises(GarminPersonalAuthenticationError, match="rejected") as error:
        client.open()

    assert "personal-secret-key" not in str(error.value)
    assert "secret key rejected" not in str(error.value)


def test_personal_client_uses_existing_rate_limit_cooldown(tmp_path):
    client, opener, limiter = _client(tmp_path, [])
    limited = GarminRateLimitError(2_000_000_000)
    limiter.record_rate_limited.return_value = limited
    opener.open.side_effect = urllib.error.HTTPError(f"{BASE_URL}{TOKEN_PATH}", 429, "limited", {}, None)

    with pytest.raises(GarminRateLimitError):
        client.open()

    limiter.record_rate_limited.assert_called_once()


def test_personal_client_rejects_missing_or_invalid_key(tmp_path):
    for key in ("", "unset", "bad key"):
        with pytest.raises(GarminPersonalAuthenticationError):
            PersonalGarminClient(key, tmp_path)
