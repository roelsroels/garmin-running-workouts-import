import pytest

from garminworkouts.models.heart_rate import HeartRateRange, validate_heart_rate_zone


def test_heart_rate_range_orders_bounds():
    assert HeartRateRange.from_config([140, 110]).to_bpm_bounds() == (110, 140)


def test_heart_rate_maximum_uses_garmin_floor():
    assert HeartRateRange.from_maximum(120).to_bpm_bounds() == (35, 120)


@pytest.mark.parametrize("value", ["fast", [100], [100, 103], [34, 120], [120, 251], [True, 120]])
def test_invalid_heart_rate_range(value):
    with pytest.raises(ValueError):
        HeartRateRange.from_config(value)


@pytest.mark.parametrize("zone", [1, 3, 5])
def test_valid_heart_rate_zone(zone):
    assert validate_heart_rate_zone(zone) == zone


@pytest.mark.parametrize("zone", [0, 6, 2.5, True])
def test_invalid_heart_rate_zone(zone):
    with pytest.raises(ValueError):
        validate_heart_rate_zone(zone)
