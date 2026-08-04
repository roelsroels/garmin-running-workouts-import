from dataclasses import dataclass


@dataclass(frozen=True)
class Pace:
    """Running pace expressed as M:SS or H:MM:SS per kilometre."""

    pace: str

    def to_seconds_per_kilometre(self):
        tokens = self.pace.strip().split(":")
        if len(tokens) not in (2, 3):
            raise ValueError(f"Unknown pace {self.pace}, expected format MM:SS or HH:MM:SS per kilometre")

        try:
            values = [int(token) for token in tokens]
        except ValueError as exc:
            raise ValueError(f"Unknown pace {self.pace}, expected numeric MM:SS per kilometre") from exc

        if len(values) == 2:
            hours = 0
            minutes, seconds = values
        else:
            hours, minutes, seconds = values

        if hours < 0 or minutes < 0 or not 0 <= seconds < 60:
            raise ValueError(f"Invalid pace {self.pace}")

        total_seconds = hours * 3600 + minutes * 60 + seconds
        if not 120 <= total_seconds <= 1800:
            raise ValueError(f"Pace must be between 2:00/km and 30:00/km but was {self.pace}")
        return total_seconds

    def to_metres_per_second(self):
        return 1000 / self.to_seconds_per_kilometre()


@dataclass(frozen=True)
class PaceRange:
    faster: Pace
    slower: Pace

    @classmethod
    def from_config(cls, value):
        if isinstance(value, str):
            tokens = [token.strip() for token in value.split("-")]
        elif isinstance(value, list | tuple):
            tokens = [str(token).strip() for token in value]
        else:
            raise ValueError("Pace must be a range such as '5:25-5:30' or ['5:25', '5:30']")

        if len(tokens) != 2:
            raise ValueError("Pace must contain exactly two bounds")

        paces = sorted((Pace(tokens[0]), Pace(tokens[1])), key=lambda pace: pace.to_seconds_per_kilometre())
        return cls(faster=paces[0], slower=paces[1])

    def to_speed_bounds(self):
        # Garmin stores pace targets as speed. The faster limit is value one.
        return self.faster.to_metres_per_second(), self.slower.to_metres_per_second()
