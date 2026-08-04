import pytest

from garminworkouts.models.pace import Pace, PaceRange


def test_pace_to_seconds_and_speed():
    pace = Pace("5:20")
    assert pace.to_seconds_per_kilometre() == 320
    assert pace.to_metres_per_second() == pytest.approx(3.125)


def test_pace_range_orders_faster_limit_first():
    pace = PaceRange.from_config(["5:30", "5:25"])
    faster, slower = pace.to_speed_bounds()
    assert faster == pytest.approx(1000 / 325)
    assert slower == pytest.approx(1000 / 330)


@pytest.mark.parametrize("pace", ["fast", "5", "1:59", "30:01", "5:99"])
def test_invalid_pace(pace):
    with pytest.raises(ValueError):
        Pace(pace).to_seconds_per_kilometre()
