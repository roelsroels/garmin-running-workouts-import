import json

import pytest

from garminworkouts.garmin.ratelimit import (
    STATE_FILENAME,
    GarminRateLimiter,
    GarminRateLimitError,
    has_reusable_tokens,
)


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(tmp_path, clock):
    return GarminRateLimiter(
        tmp_path / "tokens",
        minimum_interval=1,
        cooldown_base=900,
        cooldown_max=3600,
        clock=clock,
        sleeper=clock.sleep,
    )


def test_requests_are_spaced_and_state_is_shared_across_instances(tmp_path):
    clock = Clock()
    first = _limiter(tmp_path, clock)
    first.before_request()
    second = _limiter(tmp_path, clock)
    second.before_request()

    assert clock.sleeps == [1.0]
    state = json.loads((tmp_path / "tokens" / STATE_FILENAME).read_text())
    assert state["last_request_at"] == 1001.0


def test_429_creates_persistent_fail_fast_cooldown(tmp_path):
    clock = Clock()
    limiter = _limiter(tmp_path, clock)
    converted = limiter.record_rate_limited(RuntimeError("HTTP 429"))

    assert isinstance(converted, GarminRateLimitError)
    assert converted.retry_at == 1900.0
    with pytest.raises(GarminRateLimitError) as exc:
        _limiter(tmp_path, clock).check_cooldown()
    assert exc.value.retry_at == 1900.0
    assert clock.sleeps == []


def test_retry_after_header_can_extend_default_cooldown(tmp_path):
    clock = Clock()
    limiter = _limiter(tmp_path, clock)
    response = type("Response", (), {"headers": {"Retry-After": "1200"}})()
    error = RuntimeError("rate limited")
    error.response = response

    converted = limiter.record_rate_limited(error)

    assert converted.retry_at == 2200.0


def test_limiter_state_is_not_mistaken_for_authentication_tokens(tmp_path):
    token_store = tmp_path / "tokens"
    limiter = _limiter(tmp_path, Clock())
    limiter.before_request()
    assert not has_reusable_tokens(token_store)

    (token_store / "oauth1_token.json").write_text("{}")
    assert has_reusable_tokens(token_store)
