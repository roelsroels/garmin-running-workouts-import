"""Official Garmin Connect Developer Program OAuth and Training API client."""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from garminworkouts.garmin.provider import OFFICIAL_STARTER_PROVIDER
from garminworkouts.garmin.ratelimit import GarminRateLimiter, GarminRateLimitError

AUTHORIZATION_URL = "https://connect.garmin.com/oauth2Confirm"
TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
API_BASE_URL = "https://apis.garmin.com"
WORKOUT_CREATE_PATH = "/workoutportal/workout/v2"
WORKOUT_PATH = "/training-api/workout/v2/{workout_id}"
SCHEDULE_PATH = "/training-api/schedule/"
SCHEDULE_ITEM_PATH = "/training-api/schedule/{schedule_id}"
USER_ID_PATH = "/partner-gateway/rest/user/id"
PERMISSIONS_PATH = "/partner-gateway/rest/user/permissions"
REGISTRATION_PATH = "/partner-gateway/rest/user/registration"
TOKEN_CACHE_FILENAME = "oauth2-token.json"


class GarminStarterAPIError(RuntimeError):
    """A safe, user-facing error from Garmin's official Starter APIs."""


class GarminStarterAuthenticationError(GarminStarterAPIError):
    pass


class GarminStarterPermissionError(GarminStarterAPIError):
    pass


class GarminStarterNotFoundError(GarminStarterAPIError):
    """A previously recorded official Garmin object no longer exists."""


@dataclass(frozen=True)
class OAuthAuthorization:
    url: str
    state: str
    code_verifier: str


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        current = urllib.parse.urlsplit(request.full_url)
        target = urllib.parse.urlsplit(new_url)
        if (target.scheme, target.netloc) != (current.scheme, current.netloc):
            raise GarminStarterAPIError("Garmin returned an unexpected cross-origin redirect")
        return super().redirect_request(request, fp, code, message, headers, new_url)


class StarterGarminClient:
    """Server-side OAuth2/PKCE client for official Training API operations."""

    provider_profile = OFFICIAL_STARTER_PROVIDER

    def __init__(
        self,
        client_id,
        client_secret,
        redirect_uri,
        token_store,
        rate_limiter=None,
        opener=None,
        clock=None,
        timeout=30,
    ):
        self.client_id = _credential(client_id, "GARMIN_STARTER_CLIENT_ID", secret=False)
        self.client_secret = _credential(client_secret, "GARMIN_STARTER_CLIENT_SECRET", secret=True)
        self.redirect_uri = _redirect_uri(redirect_uri)
        self.token_store = Path(token_store).expanduser().resolve()
        self.token_path = self.token_store / TOKEN_CACHE_FILENAME
        self.rate_limiter = rate_limiter or GarminRateLimiter(self.token_store)
        self.opener = opener or urllib.request.build_opener(_SameOriginRedirectHandler())
        self.clock = clock or time.time
        self.timeout = timeout
        self._tokens = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def open(self):
        self._tokens = self._load_tokens()
        if not self._tokens:
            raise GarminStarterAuthenticationError("Connect the official Garmin Starter API account first")
        return self

    def close(self):
        self._tokens = None

    def authorization(self):
        verifier = secrets.token_urlsafe(64)[:96]
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": self.redirect_uri,
                "state": state,
            }
        )
        return OAuthAuthorization(f"{AUTHORIZATION_URL}?{query}", state, verifier)

    def exchange_code(self, code, code_verifier):
        code = _oauth_value(code, "authorization code")
        code_verifier = _oauth_value(code_verifier, "PKCE verifier")
        self._tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": self.redirect_uri,
            }
        )
        return dict(self._tokens)

    def is_connected(self):
        return bool(self._load_tokens())

    def connection_status(self):
        return {"user_id": self.user_id(), "permissions": self.permissions()}

    def user_id(self):
        response = self._request_json(USER_ID_PATH)
        user_id = response.get("userId") if isinstance(response, dict) else None
        if not user_id:
            raise GarminStarterAPIError("Garmin returned an invalid user identity response")
        return str(user_id)

    def permissions(self):
        response = self._request_json(PERMISSIONS_PATH)
        permissions = response.get("permissions") if isinstance(response, dict) else None
        if not isinstance(permissions, list):
            raise GarminStarterAPIError("Garmin returned an invalid permission response")
        return tuple(str(value) for value in permissions)

    def disconnect(self):
        try:
            self._request(REGISTRATION_PATH, method="DELETE")
        finally:
            self._delete_tokens()

    def save_workout(self, workout):
        return self._request_json(WORKOUT_CREATE_PATH, method="POST", body=workout)

    def get_workout(self, workout_id):
        return self._request_json(WORKOUT_PATH.format(workout_id=_positive_id(workout_id, "workout")))

    def update_workout(self, workout_id, workout):
        return self._request_json(
            WORKOUT_PATH.format(workout_id=_positive_id(workout_id, "workout")),
            method="PUT",
            body=workout,
        )

    def delete_workout(self, workout_id):
        return self._request(WORKOUT_PATH.format(workout_id=_positive_id(workout_id, "workout")), method="DELETE")

    def schedule_workout(self, workout_id, workout_date):
        return self._request_json(
            SCHEDULE_PATH,
            method="POST",
            body={"workoutId": _positive_id(workout_id, "workout"), "date": str(workout_date)},
        )

    def get_schedule(self, schedule_id):
        return self._request_json(SCHEDULE_ITEM_PATH.format(schedule_id=_positive_id(schedule_id, "schedule")))

    def update_schedule(self, schedule_id, workout_id, workout_date):
        return self._request_json(
            SCHEDULE_ITEM_PATH.format(schedule_id=_positive_id(schedule_id, "schedule")),
            method="PUT",
            body={
                "scheduleId": _positive_id(schedule_id, "schedule"),
                "workoutId": _positive_id(workout_id, "workout"),
                "date": str(workout_date),
            },
        )

    def unschedule_workout(self, schedule_id):
        return self._request(
            SCHEDULE_ITEM_PATH.format(schedule_id=_positive_id(schedule_id, "schedule")), method="DELETE"
        )

    def list_scheduled_workouts(self, start_date, end_date):
        query = urllib.parse.urlencode({"startDate": str(start_date), "endDate": str(end_date)})
        response = self._request_json(f"/training-api/schedule?{query}")
        if not isinstance(response, list):
            raise GarminStarterAPIError("Garmin returned an invalid workout-schedule response")
        return response

    def _ensure_access_token(self, force_refresh=False):
        if self._tokens is None:
            self._tokens = self._load_tokens()
        if not self._tokens:
            raise GarminStarterAuthenticationError("Connect the official Garmin Starter API account first")
        if force_refresh or float(self._tokens.get("expires_at", 0)) <= self.clock() + 600:
            refresh_token = self._tokens.get("refresh_token")
            if not refresh_token:
                self._delete_tokens()
                raise GarminStarterAuthenticationError("Reconnect the official Garmin Starter API account")
            self._tokens = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }
            )
        return self._tokens["access_token"]

    def _token_request(self, form):
        authorization = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode(form).encode(),
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {authorization}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        payload, _headers = self._open(request, authentication=True)
        response = _json(payload, "token")
        access_token = response.get("access_token") if isinstance(response, dict) else None
        refresh_token = response.get("refresh_token") if isinstance(response, dict) else None
        if not access_token or not refresh_token:
            raise GarminStarterAuthenticationError("Garmin returned an invalid OAuth token response")
        try:
            expires_in = max(1, int(response.get("expires_in", 0)))
            refresh_expires_in = max(1, int(response.get("refresh_token_expires_in", 0)))
        except (TypeError, ValueError) as exc:
            raise GarminStarterAuthenticationError("Garmin returned invalid OAuth token expiry values") from exc
        now = self.clock()
        tokens = {
            "version": 1,
            "access_token": str(access_token),
            "refresh_token": str(refresh_token),
            "expires_at": now + expires_in,
            "refresh_expires_at": now + refresh_expires_in,
            "scope": str(response.get("scope") or ""),
            "client_fingerprint": self._client_fingerprint,
        }
        self._save_tokens(tokens)
        return tokens

    def _request_json(self, path, method="GET", body=None):
        payload, _headers = self._request(path, method=method, body=body)
        if not payload:
            return None
        return _json(payload, "API")

    def _request(self, path, method="GET", body=None, retry_auth=True):
        if not path.startswith("/"):
            raise ValueError("Garmin API paths must be absolute")
        access_token = self._ensure_access_token()
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{API_BASE_URL}{path}", data=data, headers=headers, method=method)
        try:
            return self._open(request)
        except GarminStarterAuthenticationError:
            if not retry_auth:
                raise
            self._ensure_access_token(force_refresh=True)
            return self._request(path, method=method, body=body, retry_auth=False)

    def _open(self, request, authentication=False):
        self.rate_limiter.before_request()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
                headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise self.rate_limiter.record_rate_limited(exc) from exc
            if authentication and exc.code == 400:
                raise GarminStarterAuthenticationError("Garmin rejected or expired the OAuth authorization") from exc
            if exc.code == 401:
                raise GarminStarterAuthenticationError("Garmin rejected the official API access token") from exc
            if exc.code == 403:
                raise GarminStarterAuthenticationError(
                    "Garmin rejected the official API client or user access"
                ) from exc
            if exc.code == 412:
                raise GarminStarterPermissionError("The Garmin user has not granted the required permission") from exc
            if exc.code == 404:
                raise GarminStarterNotFoundError("The Garmin workout or schedule no longer exists") from exc
            raise GarminStarterAPIError(f"Garmin official API request failed with HTTP {exc.code}") from exc
        except GarminRateLimitError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise GarminStarterAPIError("Garmin official API could not be reached") from exc
        self.rate_limiter.record_success()
        return payload, headers

    def _load_tokens(self):
        try:
            tokens = json.loads(self.token_path.read_text())
        except (OSError, ValueError, TypeError):
            return None
        if tokens.get("client_fingerprint") != self._client_fingerprint:
            return None
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            return None
        try:
            if float(tokens.get("refresh_expires_at", 0)) <= self.clock() + 600:
                return None
        except (TypeError, ValueError):
            return None
        return tokens

    def _save_tokens(self, tokens):
        self.token_store.mkdir(parents=True, exist_ok=True)
        temporary = self.token_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(tokens, indent=2))
        try:
            os.chmod(self.token_store, 0o700)
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.token_path)

    def _delete_tokens(self):
        self._tokens = None
        try:
            self.token_path.unlink()
        except FileNotFoundError:
            pass

    @property
    def _client_fingerprint(self):
        return hashlib.sha256(self.client_id.encode()).hexdigest()


def _credential(value, environment_name, secret):
    value = str(value or "").strip()
    if not value or value.casefold() in {"unset", "none"}:
        raise GarminStarterAuthenticationError(f"Set {environment_name} before using the official Starter API")
    if len(value) > 4096 or any(character.isspace() for character in value):
        label = "secret" if secret else "client ID"
        raise GarminStarterAuthenticationError(f"The Garmin Starter {label} is invalid")
    return value


def _redirect_uri(value):
    value = str(value or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise GarminStarterAuthenticationError("GARMIN_STARTER_REDIRECT_URI must be an HTTPS URL")
    return value


def _oauth_value(value, label):
    value = str(value or "").strip()
    if not value or len(value) > 4096 or any(character.isspace() for character in value):
        raise GarminStarterAuthenticationError(f"The Garmin {label} is invalid")
    return value


def _positive_id(value, label):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Garmin {label} ID must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"Garmin {label} ID must be a positive integer")
    return value


def _json(payload, label):
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GarminStarterAPIError(f"Garmin returned an invalid {label} JSON response") from exc
