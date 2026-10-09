import asyncio
from dataclasses import dataclass

import pytest

from snmp_emulator.config import load_config
from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType
from snmp_emulator.scenarios.actions import RampAction, SequenceAction, SetAction, StepAction
from snmp_emulator.scenarios.engine import Scenario, ScenarioEngine, build_scenarios


@dataclass
class ManualClock:
    value: float = 0

    def now(self) -> float:
        return self.value


def build_store() -> MetricStore:
    return MetricStore(
        [
            Metric(
                "cpu",
                ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0"),
                SnmpDataType.GAUGE32,
                10,
            )
        ]
    )


def test_applies_set_action_at_requested_time() -> None:
    clock = ManualClock()
    store = build_store()
    engine = ScenarioEngine(store, (Scenario("load", (SetAction("cpu", 5, 80),)),), clock)
    engine.start()

    clock.value = 4.9
    engine.tick()
    assert store.get_by_name("cpu").value == 10

    clock.value = 5
    engine.tick()
    assert store.get_by_name("cpu").value == 80


def test_sequence_advances_and_repeats() -> None:
    clock = ManualClock()
    store = build_store()
    scenario = Scenario(
        "cycle",
        (SequenceAction("cpu", 0, 2, (20, 40, 60)),),
        repeat_every=6,
    )
    engine = ScenarioEngine(store, (scenario,), clock)
    engine.start()

    clock.value = 4.1
    engine.tick()
    assert store.get_by_name("cpu").value == 60

    clock.value = 6.1
    engine.tick()
    assert store.get_by_name("cpu").value == 20


def test_repeating_scenario_restores_baseline_at_cycle_boundary() -> None:
    clock = ManualClock()
    store = build_store()
    scenario = Scenario(
        "cycle",
        (
            SetAction("cpu", 2, 20),
            SetAction("cpu", 8, 90),
        ),
        repeat_every=10,
    )
    engine = ScenarioEngine(store, (scenario,), clock)
    engine.start()

    clock.value = 8
    engine.tick()
    assert store.get_by_name("cpu").value == 90

    clock.value = 10
    engine.tick()
    assert store.get_by_name("cpu").value == 10

    clock.value = 12
    engine.tick()
    assert store.get_by_name("cpu").value == 20


def test_ramp_interpolates_value() -> None:
    action = RampAction("cpu", at=2, duration=8, start=10, end=90)

    assert action.value_at(1) == (False, None)
    assert action.value_at(6) == (True, 50)
    assert action.value_at(20) == (True, 90)


def test_step_respects_maximum() -> None:
    action = StepAction("cpu", at=0, interval=2, start=10, amount=15, maximum=40)

    assert action.value_at(2)[1] == 25
    assert action.value_at(10)[1] == 40


def test_builds_every_action_type_from_configuration() -> None:
    config = load_config("examples/overload.yaml")

    scenarios = build_scenarios(config.scenarios)

    assert len(scenarios) == 1
    assert [type(action) for action in scenarios[0].actions] == [
        RampAction,
        StepAction,
        RampAction,
        SetAction,
        SetAction,
        SetAction,
    ]


def test_build_orders_actions_by_start_time() -> None:
    config = load_config("examples/device.yaml")
    scenario_config = config.scenarios[0]
    scenario_config.actions.reverse()

    scenario = build_scenarios([scenario_config])[0]

    assert [action.at for action in scenario.actions] == [2, 10]


@pytest.mark.asyncio
async def test_run_ticks_until_cancelled() -> None:
    clock = ManualClock()
    store = build_store()
    engine = ScenarioEngine(
        store,
        (Scenario("load", (SetAction("cpu", 0, 80),)),),
        clock,
        tick_interval=0.001,
    )

    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_by_name("cpu").value == 80
