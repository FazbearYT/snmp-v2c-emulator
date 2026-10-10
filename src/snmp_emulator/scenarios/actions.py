from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any


def decimal_number(value: Decimal | int | float) -> Decimal:
    return Decimal(str(value))


def decimal_divmod(dividend: Decimal, divisor: Decimal) -> tuple[int, Decimal]:
    with localcontext() as context:
        context.prec = 700
        quotient = (dividend / divisor).to_integral_value(rounding=ROUND_FLOOR)
        remainder = dividend - quotient * divisor
    return int(quotient), remainder


@dataclass(frozen=True, slots=True)
class SetAction:
    metric: str
    at: float
    value: Any

    def value_at(self, elapsed: Decimal | float) -> tuple[bool, Any]:
        return (decimal_number(elapsed) >= decimal_number(self.at), self.value)


@dataclass(frozen=True, slots=True)
class SequenceAction:
    metric: str
    at: float
    interval: float
    values: tuple[Any, ...]

    def value_at(self, elapsed: Decimal | float) -> tuple[bool, Any]:
        elapsed_value = decimal_number(elapsed)
        start_time = decimal_number(self.at)
        if elapsed_value < start_time:
            return (False, None)
        index, _ = decimal_divmod(
            elapsed_value - start_time,
            decimal_number(self.interval),
        )
        index = min(index, len(self.values) - 1)
        return (True, self.values[index])


@dataclass(frozen=True, slots=True)
class RampAction:
    metric: str
    at: float
    duration: float
    start: int
    end: int

    def value_at(self, elapsed: Decimal | float) -> tuple[bool, Any]:
        elapsed_value = decimal_number(elapsed)
        start_time = decimal_number(self.at)
        if elapsed_value < start_time:
            return (False, None)
        duration = decimal_number(self.duration)
        if elapsed_value >= start_time + duration:
            return (True, self.end)
        progress = min(
            (elapsed_value - start_time) / duration,
            Decimal(1),
        )
        value = int(
            (Decimal(self.start) + Decimal(self.end - self.start) * progress).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
        )
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

    def value_at(self, elapsed: Decimal | float) -> tuple[bool, Any]:
        elapsed_value = decimal_number(elapsed)
        start_time = decimal_number(self.at)
        if elapsed_value < start_time:
            return (False, None)
        steps, _ = decimal_divmod(
            elapsed_value - start_time,
            decimal_number(self.interval),
        )
        value = self.start + steps * self.amount
        if self.minimum is not None:
            value = max(value, self.minimum)
        if self.maximum is not None:
            value = min(value, self.maximum)
        return (True, value)


Action = SetAction | SequenceAction | RampAction | StepAction
