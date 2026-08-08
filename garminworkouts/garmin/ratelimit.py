import json
import os
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

STATE_FILENAME = ".garmin-request-limits.json"


class GarminRateLimitError(RuntimeError):
    def __init__(self, retry_at):
        self.retry_at = float(retry_at)
        local_time = datetime.fromtimestamp(self.retry_at).astimezone()
        super().__init__(
            "Garmin has rate-limited this connection. No automatic retry was attempted. "
            f"Try again after {local_time:%Y-%m-%d %H:%M:%S %Z}."
        )


def has_reusable_tokens(token_store):
    path = Path(token_store).expanduser()
    if not path.is_dir():
        return False
    return any(not item.name.startswith(STATE_FILENAME) for item in path.iterdir())


class GarminRateLimiter:
    """Persistent fail-fast cooldown plus gentle pacing for Garmin requests."""

    def __init__(
        self,
        token_store,
        minimum_interval=None,
        cooldown_base=None,
        cooldown_max=None,
        clock=None,
        sleeper=None,
    ):
        self.token_store = Path(token_store).expanduser().resolve()
        self.state_path = self.token_store / STATE_FILENAME
        self.minimum_interval = _environment_float(
            "GARMIN_REQUEST_INTERVAL_SECONDS",
            1.0 if minimum_interval is None else minimum_interval,
            minimum=0.0,
            maximum=60.0,
        )
        self.cooldown_base = _environment_float(
            "GARMIN_RATE_LIMIT_COOLDOWN_SECONDS",
            900.0 if cooldown_base is None else cooldown_base,
            minimum=60.0,
            maximum=86400.0,
        )
        self.cooldown_max = _environment_float(
            "GARMIN_RATE_LIMIT_MAX_COOLDOWN_SECONDS",
            3600.0 if cooldown_max is None else cooldown_max,
            minimum=self.cooldown_base,
            maximum=86400.0,
        )
        self.clock = clock or time.time
        self.sleeper = sleeper or time.sleep
        self._lock = threading.Lock()

    def check_cooldown(self):
        with self._lock:
            self._raise_if_blocked(self._load(), self.clock())

    def before_request(self):
        with self._lock:
            state = self._load()
            now = self.clock()
            self._raise_if_blocked(state, now)
            delay = max(0.0, state["last_request_at"] + self.minimum_interval - now)
        if delay:
            self.sleeper(delay)
        with self._lock:
            state = self._load()
            now = self.clock()
            self._raise_if_blocked(state, now)
            state["last_request_at"] = now
            self._write(state)

    def record_success(self):
        with self._lock:
            state = self._load()
            state["blocked_until"] = 0.0
            state["consecutive_429"] = 0
            self._write(state)

    def record_rate_limited(self, error):
        with self._lock:
            state = self._load()
            now = self.clock()
            if now - state["last_429_at"] > 86400:
                state["consecutive_429"] = 0
            strikes = state["consecutive_429"] + 1
            exponential = min(self.cooldown_max, self.cooldown_base * (2 ** (strikes - 1)))
            retry_after = _retry_after_seconds(error, now)
            delay = max(exponential, retry_after or 0.0)
            state["consecutive_429"] = strikes
            state["last_429_at"] = now
            state["blocked_until"] = max(state["blocked_until"], now + delay)
            self._write(state)
            return GarminRateLimitError(state["blocked_until"])

    @staticmethod
    def _raise_if_blocked(state, now):
        if state["blocked_until"] > now:
            raise GarminRateLimitError(state["blocked_until"])

    def _load(self):
        default = {
            "version": 1,
            "last_request_at": 0.0,
            "blocked_until": 0.0,
            "last_429_at": 0.0,
            "consecutive_429": 0,
        }
        try:
            value = json.loads(self.state_path.read_text())
        except (OSError, ValueError, TypeError):
            return default
        for key in ("last_request_at", "blocked_until", "last_429_at"):
            try:
                default[key] = max(0.0, float(value.get(key, 0.0)))
            except (TypeError, ValueError):
                pass
        try:
            default["consecutive_429"] = max(0, int(value.get("consecutive_429", 0)))
        except (TypeError, ValueError):
            pass
        return default

    def _write(self, state):
        self.token_store.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, indent=2))
        try:
            os.chmod(self.token_store, 0o700)
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.state_path)


def _environment_float(name, default, minimum, maximum):
    raw = os.getenv(name)
    try:
        value = float(raw) if raw not in (None, "") else float(default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _retry_after_seconds(error, now):
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            value = headers.get("Retry-After") or headers.get("retry-after")
            if value:
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    try:
                        retry_at = parsedate_to_datetime(str(value)).timestamp()
                    except (TypeError, ValueError, OverflowError):
                        pass
                    else:
                        return max(0.0, retry_at - now)
        current = current.__cause__ or current.__context__
    return None
