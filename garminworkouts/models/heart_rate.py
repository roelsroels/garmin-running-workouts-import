from dataclasses import dataclass


@dataclass(frozen=True)
class HeartRateRange:
    lower: int
    upper: int

    _MIN_BPM = 35
    _MAX_BPM = 250
    _MIN_RANGE = 5

    @classmethod
    def from_config(cls, value):
        if isinstance(value, str):
            tokens = [token.strip() for token in value.split("-")]
        elif isinstance(value, list | tuple):
            tokens = list(value)
        else:
            raise ValueError("Heart rate must be a range such as '110-130' or [110, 130]")

        if len(tokens) != 2:
            raise ValueError("Heart rate must contain exactly two BPM bounds")
        if any(isinstance(token, bool) for token in tokens):
            raise ValueError("Heart-rate bounds must be whole BPM values")

        try:
            bounds = sorted(int(token) for token in tokens)
        except (TypeError, ValueError) as exc:
            raise ValueError("Heart-rate bounds must be whole BPM values") from exc
        heart_rate = cls(lower=bounds[0], upper=bounds[1])
        heart_rate.validate()
        return heart_rate

    @classmethod
    def from_maximum(cls, value):
        if isinstance(value, bool):
            raise ValueError("Maximum heart rate must be a whole BPM value")
        try:
            maximum = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Maximum heart rate must be a whole BPM value") from exc
        heart_rate = cls(lower=cls._MIN_BPM, upper=maximum)
        heart_rate.validate()
        return heart_rate

    def validate(self):
        if not self._MIN_BPM <= self.lower <= self._MAX_BPM:
            raise ValueError(f"Lower heart rate must be between {self._MIN_BPM} and {self._MAX_BPM} bpm")
        if not self._MIN_BPM <= self.upper <= self._MAX_BPM:
            raise ValueError(f"Upper heart rate must be between {self._MIN_BPM} and {self._MAX_BPM} bpm")
        if self.upper - self.lower < self._MIN_RANGE:
            raise ValueError(f"Heart-rate bounds must be at least {self._MIN_RANGE} bpm apart")
        return self

    def to_bpm_bounds(self):
        return self.lower, self.upper


def validate_heart_rate_zone(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
        raise ValueError("Heart-rate zone must be an integer between 1 and 5")
    return value
