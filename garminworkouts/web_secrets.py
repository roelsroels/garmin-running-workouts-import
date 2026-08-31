"""Short-lived API keys: server memory only, scoped to a browser and LLM configuration."""

import secrets
import time
from threading import Lock


class SessionAPIKeys:
    def __init__(self, ttl=3600, capacity=128):
        self.ttl = ttl
        self.capacity = capacity
        self._entries = {}
        self._lock = Lock()

    def _purge(self):
        now = time.monotonic()
        self._entries = {token: entry for token, entry in self._entries.items() if entry[2] > now}

    def put(self, config, api_key):
        with self._lock:
            self._purge()
            if len(self._entries) >= self.capacity:
                del self._entries[next(iter(self._entries))]
            token = secrets.token_urlsafe(32)
            self._entries[token] = (config, api_key, time.monotonic() + self.ttl)
            return token

    def get(self, token, config):
        with self._lock:
            self._purge()
            entry = self._entries.get(token)
            if entry and entry[0] == config:
                return entry[1]
            self._entries.pop(token, None)
            return None

    def remove(self, token):
        with self._lock:
            self._entries.pop(token, None)


class PendingGarminLogins:
    """Short-lived Garmin MFA sessions kept only in this server process."""

    def __init__(self, ttl=300, capacity=32, maximum_attempts=3):
        self.ttl = ttl
        self.capacity = capacity
        self.maximum_attempts = maximum_attempts
        self._entries = {}
        self._lock = Lock()

    def _purge(self):
        now = time.monotonic()
        expired = [token for token, entry in self._entries.items() if entry[2] <= now]
        for token in expired:
            self._remove(token)

    def put(self, username, connection):
        with self._lock:
            self._purge()
            if len(self._entries) >= self.capacity:
                self._remove(next(iter(self._entries)))
            token = secrets.token_urlsafe(32)
            self._entries[token] = (
                str(username),
                connection,
                time.monotonic() + self.ttl,
                self.maximum_attempts,
            )
            return token

    def get(self, token):
        with self._lock:
            self._purge()
            entry = self._entries.get(token)
            return (entry[0], entry[1], entry[3]) if entry else None

    def record_failure(self, token):
        with self._lock:
            self._purge()
            entry = self._entries.get(token)
            if not entry:
                return 0
            remaining = entry[3] - 1
            if remaining <= 0:
                self._remove(token)
                return 0
            self._entries[token] = (*entry[:3], remaining)
            return remaining

    def remove(self, token):
        with self._lock:
            self._remove(token)

    def _remove(self, token):
        entry = self._entries.pop(token, None)
        if entry:
            entry[1].close()
