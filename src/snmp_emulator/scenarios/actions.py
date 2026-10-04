from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SetAction:
    metric: str
    at: float
    value: Any

    def value_at(self, elapsed: float) -> tuple[bool, Any]:
        return (elapsed >= self.at, self.value)


@dataclass(frozen=True, slots=True)
class SequenceAction:
    metric: str
    at: float
    interval: float
    values: tuple[Any, ...]

    def value_at(self, elapsed: float) -> tuple[bool, Any]:
        if elapsed < self.at:
            return (False, None)
        index = min(int((elapsed - self.at) // self.interval), len(self.values) - 1)
        return (True, self.values[index])


Action = SetAction | SequenceAction


# TODO: add continuous ramp and fixed-step actions after timeline semantics settle.
