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


@dataclass(frozen=True, slots=True)
class RampAction:
    metric: str
    at: float
    duration: float
    start: int
    end: int

    def value_at(self, elapsed: float) -> tuple[bool, Any]:
        if elapsed < self.at:
            return (False, None)
        progress = min((elapsed - self.at) / self.duration, 1.0)
        value = round(self.start + (self.end - self.start) * progress)
        return (True, value)


@dataclass(frozen=True, slots=True)
class StepAction:
    metric: str
    at: float
    interval: float
    start: int
    amount: int
    minimum: int | None = None
    maximum: int | None = None

    def value_at(self, elapsed: float) -> tuple[bool, Any]:
        if elapsed < self.at:
            return (False, None)
        steps = int((elapsed - self.at) // self.interval)
        value = self.start + steps * self.amount
        if self.minimum is not None:
            value = max(value, self.minimum)
        if self.maximum is not None:
            value = min(value, self.maximum)
        return (True, value)


Action = SetAction | SequenceAction | RampAction | StepAction
