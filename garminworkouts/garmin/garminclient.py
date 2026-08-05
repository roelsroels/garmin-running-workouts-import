class GarminClient:
    def __init__(self, username, password, token_store):
        self.username = username
        self.password = password
        self.token_store = token_store

    def __enter__(self):
        from garminconnect import Garmin

        self.session = Garmin(self.username, self.password)
        self.session.login(tokenstore=self.token_store)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.session = None

    def list_workouts(self, batch_size=100):
        start_index = 0
        while True:
            workouts = self.session.get_workouts(start=start_index, limit=batch_size)
            if not workouts:
                break
            yield from workouts
            if len(workouts) < batch_size:
                break
            start_index += batch_size

    def get_workout(self, workout_id):
        return self.session.get_workout_by_id(workout_id)

    def download_workout(self, workout_id, file):
        content = self.session.download_workout(workout_id)
        with open(file, "wb") as output:
            output.write(content)

    def save_workout(self, workout):
        return self.session.upload_workout(workout)

    def update_workout(self, workout_id, workout):
        return self.session.update_workout(workout_id, workout)

    def delete_workout(self, workout_id):
        return self.session.delete_workout(workout_id)

    def schedule_workout(self, workout_id, date):
        return self.session.schedule_workout(workout_id, date)

    def unschedule_workout(self, scheduled_workout_id):
        return self.session.unschedule_workout(scheduled_workout_id)

    def list_scheduled_workouts(self, year, month):
        if not 1 <= int(month) <= 12:
            raise ValueError(f"Month must be between 1 and 12 but was {month}")
        return self.session.get_scheduled_workouts(year, month) or []

    def list_recent_activities(self, limit=20, activity_type="running"):
        return self.session.get_activities(start=0, limit=limit, activitytype=activity_type) or []

    def list_activities_by_date(self, start_date, end_date, activity_type="running"):
        return self.session.get_activities_by_date(
            start_date,
            end_date,
            activitytype=activity_type,
            sortorder="desc",
        )

    def download_activity_original(self, activity_id):
        download_format = self.session.ActivityDownloadFormat.ORIGINAL
        return self.session.download_activity(activity_id, dl_fmt=download_format)
