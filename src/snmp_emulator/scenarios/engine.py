from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..config import (
    RampActionConfig,
    ScenarioConfig,
    SequenceActionConfig,
    SetActionConfig,
    StepActionConfig,
)
from ..domain.store import MetricStore
from .actions import Action, RampAction, SequenceAction, SetAction, StepAction
from .clock import Clock, MonotonicClock


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    actions: tuple[Action, ...]
    repeat_every: float | None = None


def build_scenarios(configs: list[ScenarioConfig]) -> tuple[Scenario, ...]:
    scenarios: list[Scenario] = []
    for item in configs:
        actions: list[Action] = []
        for action in item.actions:
            if isinstance(action, SetActionConfig):
                actions.append(SetAction(action.metric, action.at, action.value))
            elif isinstance(action, SequenceActionConfig):
                actions.append(
                    SequenceAction(
                        action.metric,
                        action.at,
                        action.interval,
                        tuple(action.values),
                    )
                )
            elif isinstance(action, RampActionConfig):
                actions.append(
                    RampAction(
                        action.metric,
                        action.at,
                        action.duration,
                        action.start,
                        action.end,
                    )
                )
            elif isinstance(action, StepActionConfig):
                actions.append(
                    StepAction(
                        action.metric,
                        action.at,
                        action.interval,
                        action.start,
                        action.amount,
                        action.minimum,
                        action.maximum,
                    )
                )
        scenarios.append(Scenario(item.name, tuple(actions), item.repeat_every))
    return tuple(scenarios)


class ScenarioEngine:
    def __init__(
        self,
        store: MetricStore,
        scenarios: tuple[Scenario, ...],
        clock: Clock | None = None,
        tick_interval: float = 0.1,
    ) -> None:
        self.store = store
        self.scenarios = scenarios
        self.clock = clock or MonotonicClock()
        self.tick_interval = tick_interval
        self._started_at: float | None = None

    def start(self) -> None:
        self._started_at = self.clock.now()

    def tick(self) -> None:
        if self._started_at is None:
            self.start()
        elapsed = self.clock.now() - self._started_at
        for scenario in self.scenarios:
            local_elapsed = elapsed
            if scenario.repeat_every is not None:
                local_elapsed %= scenario.repeat_every
            for action in scenario.actions:
                active, value = action.value_at(local_elapsed)
                if active:
                    self.store.update(action.metric, value)

    async def run(self) -> None:
        self.start()
        while True:
            self.tick()
            await asyncio.sleep(self.tick_interval)
