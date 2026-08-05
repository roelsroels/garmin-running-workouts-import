from unittest.mock import MagicMock, mock_open, patch

import pytest

from garminworkouts.garmin.garminclient import GarminClient


def _client_with_session():
    client = GarminClient("user", "password", ".tokens")
    client.session = MagicMock()
    return client


@patch("garminconnect.Garmin")
def test_context_logs_in_with_token_store(garmin):
    session = garmin.return_value
    with GarminClient("user", "password", ".tokens") as connection:
        assert connection.session is session
    garmin.assert_called_once_with("user", "password")
    session.login.assert_called_once_with(tokenstore=".tokens")


def test_list_workouts_paginates():
    client = _client_with_session()
    first = [{"workoutId": 1}, {"workoutId": 2}]
    client.session.get_workouts.side_effect = [first, []]
    assert list(client.list_workouts(batch_size=2)) == first
    assert client.session.get_workouts.call_count == 2


def test_list_workouts_stops_after_short_page():
    client = _client_with_session()
    client.session.get_workouts.return_value = [{"workoutId": 1}]
    assert list(client.list_workouts(batch_size=2)) == [{"workoutId": 1}]
    client.session.get_workouts.assert_called_once_with(start=0, limit=2)


def test_workout_operations_delegate_to_current_api():
    client = _client_with_session()
    payload = {"workoutName": "Run"}

    client.get_workout(7)
    client.save_workout(payload)
    client.update_workout(7, payload)
    client.delete_workout(7)
    client.schedule_workout(7, "2026-08-11")
    client.list_scheduled_workouts(2026, 8)

    client.session.get_workout_by_id.assert_called_once_with(7)
    client.session.upload_workout.assert_called_once_with(payload)
    client.session.update_workout.assert_called_once_with(7, payload)
    client.session.delete_workout.assert_called_once_with(7)
    client.session.schedule_workout.assert_called_once_with(7, "2026-08-11")
    client.session.get_scheduled_workouts.assert_called_once_with(2026, 8)


def test_download_workout_writes_bytes():
    client = _client_with_session()
    client.session.download_workout.return_value = b"fit"
    with patch("builtins.open", mock_open()) as output:
        client.download_workout(7, "run.fit")
    output.assert_called_once_with("run.fit", "wb")
    output().write.assert_called_once_with(b"fit")


def test_list_scheduled_workouts_rejects_invalid_month():
    client = _client_with_session()
    with pytest.raises(ValueError, match="Month must be between"):
        client.list_scheduled_workouts(2026, 13)


def test_activity_operations_delegate_to_current_api():
    client = _client_with_session()
    client.session.ActivityDownloadFormat.ORIGINAL = "original"
    client.session.get_activities.return_value = [{"activityId": 1}]
    client.session.get_activities_by_date.return_value = [{"activityId": 2}]
    client.session.download_activity.return_value = b"original"

    assert client.list_recent_activities(12) == [{"activityId": 1}]
    assert client.list_activities_by_date("2026-07-01", "2026-08-01") == [{"activityId": 2}]
    assert client.download_activity_original(2) == b"original"

    client.session.get_activities.assert_called_once_with(start=0, limit=12, activitytype="running")
    client.session.get_activities_by_date.assert_called_once_with(
        "2026-07-01",
        "2026-08-01",
        activitytype="running",
        sortorder="desc",
    )
    client.session.download_activity.assert_called_once_with(2, dl_fmt="original")
