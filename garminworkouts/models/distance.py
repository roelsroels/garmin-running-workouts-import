import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Distance:
    distance: str | int | float

    def to_metres(self):
        if isinstance(self.distance, bool):
            raise ValueError("Distance must be a number or a value such as '400m' or '10km'")
        if isinstance(self.distance, int | float):
            metres = float(self.distance)
        else:
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|km)?\s*", str(self.distance), re.IGNORECASE)
            if not match:
                raise ValueError(f"Unknown distance {self.distance}, expected metres or kilometres")
            amount = float(match.group(1))
            metres = amount * 1000 if (match.group(2) or "m").lower() == "km" else amount

        if not 1 <= metres <= 500_000:
            raise ValueError(f"Distance must be between 1m and 500km but was {self.distance}")
        return metres
