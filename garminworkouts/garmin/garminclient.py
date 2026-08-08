import os

from garminconnect import GarminConnectTooManyRequestsError

from garminworkouts.garmin.ratelimit import GarminRateLimiter

LOGIN_STRATEGIES = {
    "mobile+cffi",
    "mobile+requests",
    "widget+cffi",
    "portal+cffi",
    "portal+requests",
}


class GarminClient:
    def __init__(self, username, password, token_store, rate_limiter=None):
        self.username = username
        self.password = password
        self.token_store = token_store
        self.rate_limiter = rate_limiter or GarminRateLimiter(token_store)

    def __enter__(self):
        from garminconnect import Garmin

        self.rate_limiter.before_request()
        self.session = Garmin(self.username, self.password)
        self._configure_login_strategy()
        try:
            self.session.login(tokenstore=self.token_store)
        except Exception as exc:
            self.session = None
            self._raise_if_rate_limited(exc)
            raise
        self.rate_limiter.record_success()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.session = None

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


def _is_rate_limited(error):
    if isinstance(error, GarminConnectTooManyRequestsError):
        return True
    message = str(error).casefold()
    return "429" in message or "too many" in message or "rate limit" in message
