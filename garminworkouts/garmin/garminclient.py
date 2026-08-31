import os
from pathlib import Path

from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

from garminworkouts.garmin.ratelimit import GarminRateLimiter

LOGIN_STRATEGIES = {
    "mobile+cffi",
    "mobile+requests",
    "widget+cffi",
    "portal+cffi",
    "portal+requests",
}


class GarminAuthenticationError(ValueError):
    """A safe, user-facing Garmin authentication failure."""


class GarminMFARequiredError(GarminAuthenticationError):
    """Garmin requires a one-time code but this login cannot prompt for one."""


class GarminTokenPersistenceError(GarminAuthenticationError):
    """Garmin authenticated, but the resulting reusable tokens were not saved."""


class GarminClient:
    def __init__(
        self,
        username,
        password,
        token_store,
        rate_limiter=None,
        prompt_mfa=None,
        defer_mfa=False,
    ):
        self.username = username
        self.password = password
        self.token_store = token_store
        self.rate_limiter = rate_limiter or GarminRateLimiter(token_store)
        self.prompt_mfa = prompt_mfa
        self.defer_mfa = defer_mfa
        self.session = None
        self.mfa_required = False

    def __enter__(self):
        self.open()
        if self.mfa_required:
            self.close()
            raise GarminMFARequiredError(
                "Garmin requires a verification code. Use the interactive CLI or web login to enter it."
            )
        return self

    def open(self):
        from garminconnect import Garmin

        self.rate_limiter.before_request()
        options = {}
        if self.prompt_mfa is not None:
            options["prompt_mfa"] = self.prompt_mfa
        if self.defer_mfa:
            options["return_on_mfa"] = True
        self.session = Garmin(self.username, self.password, **options)
        self._configure_login_strategy()
        try:
            result = self.session.login(tokenstore=None if self.defer_mfa else self.token_store)
        except Exception as exc:
            self.close()
            self._raise_if_rate_limited(exc)
            self._raise_if_authentication_failed(exc)
            raise
        self.mfa_required = bool(result and result[0] == "needs_mfa")
        if self.mfa_required:
            self._discard_password()
            return self
        if self.defer_mfa:
            self._discard_password()
            try:
                self._persist_tokens()
            except Exception:
                self.close()
                raise
        self.rate_limiter.record_success()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self.session = None

    def resume_mfa(self, code):
        code = str(code or "").strip()
        if not self.session or not self.mfa_required:
            raise GarminAuthenticationError("The Garmin verification request is no longer active")
        if not code or len(code) > 32 or any(character.isspace() for character in code):
            raise GarminAuthenticationError("Enter the verification code supplied by Garmin")
        self.rate_limiter.before_request()
        try:
            self.session.resume_login({}, code)
            self._persist_tokens()
        except Exception as exc:
            self._raise_if_rate_limited(exc)
            self._raise_if_authentication_failed(exc, mfa=True)
            raise
        self.mfa_required = False
        self.rate_limiter.record_success()
        return self

    def list_workouts(self, batch_size=100):
        start_index = 0
        while True:
            workouts = self._request(self.session.get_workouts, start=start_index, limit=batch_size)
            if not workouts:
                break
            yield from workouts
            if len(workouts) < batch_size:
                break
            start_index += batch_size

    def get_workout(self, workout_id):
        return self._request(self.session.get_workout_by_id, workout_id)

    def download_workout(self, workout_id, file):
        content = self._request(self.session.download_workout, workout_id)
        with open(file, "wb") as output:
            output.write(content)

    def save_workout(self, workout):
        return self._request(self.session.upload_workout, workout)

    def update_workout(self, workout_id, workout):
        return self._request(self.session.update_workout, workout_id, workout)

    def delete_workout(self, workout_id):
        return self._request(self.session.delete_workout, workout_id)

    def schedule_workout(self, workout_id, date):
        return self._request(self.session.schedule_workout, workout_id, date)

    def unschedule_workout(self, scheduled_workout_id):
        return self._request(self.session.unschedule_workout, scheduled_workout_id)

    def list_scheduled_workouts(self, year, month):
        if not 1 <= int(month) <= 12:
            raise ValueError(f"Month must be between 1 and 12 but was {month}")
        return self._request(self.session.get_scheduled_workouts, year, month) or []

    def list_recent_activities(self, limit=20, activity_type="running"):
        return (
            self._request(
                self.session.get_activities,
                start=0,
                limit=limit,
                activitytype=activity_type,
            )
            or []
        )

    def list_activities_by_date(self, start_date, end_date, activity_type="running"):
        return self._request(
            self.session.get_activities_by_date,
            start_date,
            end_date,
            activitytype=activity_type,
            sortorder="desc",
        )

    def download_activity_original(self, activity_id):
        download_format = self.session.ActivityDownloadFormat.ORIGINAL
        return self._request(self.session.download_activity, activity_id, dl_fmt=download_format)

    def _request(self, operation, *args, **kwargs):
        self.rate_limiter.before_request()
        try:
            result = operation(*args, **kwargs)
        except Exception as exc:
            self._raise_if_rate_limited(exc)
            raise
        self.rate_limiter.record_success()
        return result

    def _configure_login_strategy(self):
        strategy = os.getenv("GARMIN_LOGIN_STRATEGY", "mobile+requests").strip().casefold()
        if strategy not in LOGIN_STRATEGIES:
            supported = ", ".join(sorted(LOGIN_STRATEGIES))
            raise ValueError(f"GARMIN_LOGIN_STRATEGY must be one of: {supported}")
        client = getattr(self.session, "client", None)
        if client is not None and hasattr(client, "skip_strategies"):
            client.skip_strategies = LOGIN_STRATEGIES - {strategy}

    def _raise_if_rate_limited(self, error):
        if _is_rate_limited(error):
            raise self.rate_limiter.record_rate_limited(error) from error

    def _raise_if_authentication_failed(self, error, mfa=False):
        if not isinstance(error, GarminConnectAuthenticationError):
            return
        message = str(error).casefold()
        if "mfa required" in message:
            raise GarminMFARequiredError(
                "Garmin requires a verification code. Use the interactive CLI or web login to enter it."
            ) from error
        if mfa or "mfa" in message or "verification code" in message:
            raise GarminAuthenticationError(
                "Garmin rejected the verification code. Check the latest code and try again."
            ) from error
        raise GarminAuthenticationError(
            "Garmin rejected the login. Check the account name and password, then try again."
        ) from error

    def _persist_tokens(self):
        client = getattr(self.session, "client", None)
        if client is None or not hasattr(client, "dump"):
            raise GarminTokenPersistenceError("Garmin authenticated, but reusable session tokens could not be saved")
        token_store = str(Path(self.token_store).expanduser().resolve())
        client._tokenstore_path = token_store
        try:
            client.dump(token_store)
        except Exception as error:
            raise GarminTokenPersistenceError(
                "Garmin authenticated, but reusable session tokens could not be saved"
            ) from error

    def _discard_password(self):
        self.password = None
        if self.session is not None and hasattr(self.session, "password"):
            self.session.password = None


def _is_rate_limited(error):
    if isinstance(error, GarminConnectTooManyRequestsError):
        return True
    message = str(error).casefold()
    return "429" in message or "too many" in message or "rate limit" in message
