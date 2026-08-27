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
