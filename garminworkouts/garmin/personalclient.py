import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from garminworkouts.garmin.provider import OFFICIAL_PERSONAL_PROVIDER
from garminworkouts.garmin.ratelimit import GarminRateLimiter, GarminRateLimitError

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
ACTIVITY_SEARCH_PATH = "/activitylist-service/activities/search/activities"
ACTIVITY_DOWNLOAD_PATH = "/download-service/files/activity/{activity_id}"
TOKEN_CACHE_FILENAME = "access-token.json"


class GarminPersonalAPIError(RuntimeError):
    """A safe, user-facing error from Garmin's official Personal API."""


class GarminPersonalAuthenticationError(GarminPersonalAPIError):
    pass


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        current = urllib.parse.urlsplit(request.full_url)
        target = urllib.parse.urlsplit(new_url)
        if (target.scheme, target.netloc) != (current.scheme, current.netloc):
            raise GarminPersonalAPIError("Garmin returned an unexpected cross-origin redirect")
        return super().redirect_request(request, fp, code, message, headers, new_url)


class PersonalGarminClient:
    """Official Garmin Connect Personal API activity reader.

    The Personal API is intentionally limited to activity summaries and original
    activity downloads. Workout creation and calendar scheduling remain the job
    of a separate training publisher.
    """

    provider_profile = OFFICIAL_PERSONAL_PROVIDER

    def __init__(
        self,
        api_key,
        token_store,
        rate_limiter=None,
        opener=None,
        clock=None,
        timeout=30,
    ):
        self.api_key = str(api_key or "").strip()
        if not self.api_key or self.api_key.casefold() in {"unset", "none"}:
            raise GarminPersonalAuthenticationError(
                "Set GARMIN_PERSONAL_API_KEY before selecting the official Personal API"
            )
        if len(self.api_key) > 4096 or any(character.isspace() for character in self.api_key):
            raise GarminPersonalAuthenticationError("GARMIN_PERSONAL_API_KEY is not a valid API key")
        self.token_store = Path(token_store).expanduser().resolve()
        self.token_path = self.token_store / TOKEN_CACHE_FILENAME
        self.rate_limiter = rate_limiter or GarminRateLimiter(self.token_store)
        self.opener = opener or urllib.request.build_opener(_SameOriginRedirectHandler())
        self.clock = clock or time.time
        self.timeout = timeout
        self._access_token = None
        self._expires_at = 0.0

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def open(self):
        cached = self._load_cached_token()
        if cached:
            self._access_token, self._expires_at = cached
        else:
            self._mint_access_token()
        return self

    def close(self):
        self._access_token = None
        self._expires_at = 0.0

    def list_recent_activities(self, limit=20, activity_type="running"):
        limit = int(limit)
        if limit < 1:
            raise ValueError("Activity limit must be at least 1")
        return self._search_activities(limit=limit, activity_type=activity_type)

    def list_activities_by_date(self, start_date, end_date, activity_type="running"):
        return self._search_activities(
            activity_type=activity_type,
            start_date=str(start_date),
            end_date=str(end_date),
        )

    def download_activity_original(self, activity_id):
        activity_id = str(activity_id)
        if not activity_id.isdigit():
            raise ValueError("Garmin activity ID must be numeric")
        return self._request_bytes(ACTIVITY_DOWNLOAD_PATH.format(activity_id=activity_id))

    def _search_activities(self, limit=None, activity_type="running", start_date=None, end_date=None):
        results = []
        start = 0
        while limit is None or len(results) < limit:
            page_limit = min(99, limit - len(results)) if limit is not None else 99
            query = {
                "start": start,
                "limit": page_limit,
                "sortOrder": "desc",
                "excludeChildren": "true",
                "activityType": activity_type,
            }
            if start_date:
                query["startDate"] = start_date
            if end_date:
                query["endDate"] = end_date
            page = self._request_json(f"{ACTIVITY_SEARCH_PATH}?{urllib.parse.urlencode(query)}")
            if not isinstance(page, list):
                raise GarminPersonalAPIError("Garmin returned an invalid activity-list response")
            results.extend(item for item in page if isinstance(item, dict))
            if len(page) < page_limit:
                break
            start += page_limit
        return results[:limit] if limit is not None else results

    def _mint_access_token(self):
        payload, headers = self._request(
            TOKEN_PATH,
            method="POST",
            authorization=f"ApiKey {self.api_key}",
        )
        token, expires_in = _parse_token_response(payload)
        now = self.clock()
        jwt_expiry = _jwt_expiration(token)
        documented_expiry = now + 24 * 60 * 60
        response_expiry = now + expires_in if expires_in else documented_expiry
        self._access_token = token
        self._expires_at = min(value for value in (response_expiry, jwt_expiry) if value is not None)
        self._save_cached_token(headers)

    def _ensure_access_token(self):
        if not self._access_token or self._expires_at <= self.clock() + 60:
            self._mint_access_token()

    def _request_json(self, path):
        payload, _headers = self._authorized_request(path)
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GarminPersonalAPIError("Garmin returned an invalid JSON response") from exc

    def _request_bytes(self, path):
        payload, _headers = self._authorized_request(path)
        return payload

    def _authorized_request(self, path):
        self._ensure_access_token()
        try:
            return self._request(path, authorization=f"Bearer {self._access_token}")
        except GarminPersonalAuthenticationError:
            # A cached token may have been revoked early. Mint once and replay once.
            self._delete_cached_token()
            self._mint_access_token()
            return self._request(path, authorization=f"Bearer {self._access_token}")

    def _request(self, path, method="GET", authorization=None):
        if not path.startswith("/"):
            raise ValueError("Garmin Personal API paths must be absolute")
        url = f"{BASE_URL}{path}"
        headers = {"Accept": "application/json, application/x-zip-compressed"}
        if authorization:
            headers["Authorization"] = authorization
        request = urllib.request.Request(url, headers=headers, method=method)
        self.rate_limiter.before_request()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise self.rate_limiter.record_rate_limited(exc) from exc
            if exc.code in {401, 403}:
                raise GarminPersonalAuthenticationError(
                    "Garmin rejected the Personal API credentials or access scope"
                ) from exc
            raise GarminPersonalAPIError(f"Garmin Personal API request failed with HTTP {exc.code}") from exc
        except GarminRateLimitError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise GarminPersonalAPIError("Garmin Personal API could not be reached") from exc
        self.rate_limiter.record_success()
        return payload, response_headers

    def _load_cached_token(self):
        try:
            value = json.loads(self.token_path.read_text())
        except (OSError, ValueError, TypeError):
            return None
        if value.get("key_fingerprint") != self._key_fingerprint:
            return None
        token = value.get("access_token")
        try:
            expires_at = float(value.get("expires_at", 0))
        except (TypeError, ValueError):
            return None
        if not token or expires_at <= self.clock() + 60:
            return None
        return str(token), expires_at

    def _save_cached_token(self, _headers):
        self.token_store.mkdir(parents=True, exist_ok=True)
        temporary = self.token_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "access_token": self._access_token,
                    "expires_at": self._expires_at,
                    "key_fingerprint": self._key_fingerprint,
                },
                indent=2,
            )
        )
        try:
            os.chmod(self.token_store, 0o700)
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.token_path)

    def _delete_cached_token(self):
        self._access_token = None
        self._expires_at = 0.0
        try:
            self.token_path.unlink()
        except FileNotFoundError:
            pass

    @property
    def _key_fingerprint(self):
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()


def _parse_token_response(payload):
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = payload.decode("utf-8", errors="replace").strip()
    expires_in = None
    if isinstance(value, dict):
        token = value.get("access_token") or value.get("accessToken") or value.get("token")
        raw_expiry = value.get("expires_in") or value.get("expiresIn")
        try:
            expires_in = max(1.0, float(raw_expiry)) if raw_expiry is not None else None
        except (TypeError, ValueError):
            expires_in = None
    elif isinstance(value, str):
        token = value
    else:
        token = None
    if not token or not isinstance(token, str) or any(character.isspace() for character in token):
        raise GarminPersonalAuthenticationError("Garmin returned an invalid Personal API access token")
    return token, expires_in


def _jwt_expiration(token):
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return float(value["exp"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
