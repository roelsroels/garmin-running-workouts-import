import pytest

from garminworkouts.models.distance import Distance


@pytest.mark.parametrize(("value", "expected"), [(400, 400), ("400m", 400), ("1.5 km", 1500)])
def test_distance_to_metres(value, expected):
    assert Distance(value).to_metres() == expected


@pytest.mark.parametrize("value", [True, "far", 0, "501km"])
def test_invalid_distance(value):
    with pytest.raises(ValueError):
        Distance(value).to_metres()
