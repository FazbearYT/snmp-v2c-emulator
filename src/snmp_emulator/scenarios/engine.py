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
        for action in sorted(item.actions, key=lambda candidate: candidate.at):
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
        self._scenario_baselines: dict[str, dict[str, object]] = {}
        self._scenario_cycles: dict[str, int] = {}

    def start(self) -> None:
        self._started_at = self.clock.now()
        self._scenario_baselines = {}
        self._scenario_cycles = {}
        for scenario in self.scenarios:
            if scenario.repeat_every is None:
                continue
            baseline: dict[str, object] = {}
            for action in scenario.actions:
                metric = self.store.get_by_name(action.metric)
                if metric is not None:
                    baseline[action.metric] = metric.value
            self._scenario_baselines[scenario.name] = baseline
            self._scenario_cycles[scenario.name] = 0

    def tick(self) -> None:
        if self._started_at is None:
            self.start()
        elapsed = self.clock.now() - self._started_at
        for scenario in self.scenarios:
            local_elapsed = elapsed
            if scenario.repeat_every is not None:
                cycle = int(elapsed // scenario.repeat_every)
                if cycle != self._scenario_cycles[scenario.name]:
                    for metric_name, value in self._scenario_baselines[scenario.name].items():
                        self.store.update(metric_name, value)
                    self._scenario_cycles[scenario.name] = cycle
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
