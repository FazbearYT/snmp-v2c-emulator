import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from snmp_emulator.application import build_store
from snmp_emulator.config import load_config
from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType
from snmp_emulator.json_config import load_json_config
from snmp_emulator.scenarios.actions import SetAction, StepAction
from snmp_emulator.scenarios.engine import Scenario, ScenarioEngine, build_scenarios

OID = "1.3.6.1.4.1.55555.1.0"
EXAMPLES = Path(__file__).parents[1] / "examples"


@dataclass
class ManualClock:
    value: float = 0

    def now(self) -> float:
        return self.value


@pytest.fixture
def metric_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger="snmp_emulator.scenarios.engine")
    return caplog


def numeric_store() -> MetricStore:
    return MetricStore([Metric("cpu", ObjectIdentifier.parse(OID), SnmpDataType.INTEGER32, 10)])


@pytest.mark.parametrize("final_value", [30, 10])
def test_logs_only_net_change_and_does_not_repeat_on_later_ticks(metric_logs, final_value) -> None:
    store = numeric_store()
    clock = ManualClock()
    engine = ScenarioEngine(
        store,
        (Scenario("load", (SetAction("cpu", 1, 20), SetAction("cpu", 2, final_value))),),
        clock,
    )
    engine.start()
    clock.value = 2

    engine.tick()

    assert store.get_by_name("cpu").value == final_value
    expected = (
        [f"metric cpu oid={OID} changed: 10 -> 30 (scenario=load)"] if final_value == 30 else []
    )
    assert metric_logs.messages == expected
    assert all(record.levelno == logging.INFO for record in metric_logs.records)
    metric_logs.clear()
    clock.value = 3

    engine.tick()

    assert store.get_by_name("cpu").value == final_value
    assert metric_logs.records == []


def test_initial_loading_and_start_are_silent_but_first_tick_change_is_logged(metric_logs) -> None:
    store = numeric_store()
    engine = ScenarioEngine(store, (Scenario("initial-rule", (SetAction("cpu", 0, 20),)),))
    engine.start()

    assert metric_logs.records == []

    engine.tick()

    assert metric_logs.messages == [
        f"metric cpu oid={OID} changed: 10 -> 20 (scenario=initial-rule)"
    ]


def test_unchanged_first_tick_and_inactive_actions_are_silent(metric_logs) -> None:
    engine = ScenarioEngine(
        numeric_store(),
        (Scenario("stable", (SetAction("cpu", 0, 10), SetAction("cpu", 5, 80))),),
        ManualClock(),
    )

    engine.tick()

    assert metric_logs.records == []


@pytest.mark.parametrize("peak", [90, 20])
def test_repeat_reset_and_actions_are_aggregated_together(metric_logs, peak) -> None:
    store = numeric_store()
    clock = ManualClock()
    engine = ScenarioEngine(
        store,
        (Scenario("cycle", (SetAction("cpu", 0, 20), SetAction("cpu", 8, peak)), 10),),
        clock,
    )
    engine.start()
    clock.value = 8
    engine.tick()
    metric_logs.clear()
    clock.value = 10

    engine.tick()

    assert store.get_by_name("cpu").value == 20
    assert metric_logs.messages == (
        [f"metric cpu oid={OID} changed: 90 -> 20 (scenario=cycle)"] if peak == 90 else []
    )


def test_repeat_reset_without_active_action_is_logged(metric_logs) -> None:
    store = numeric_store()
    clock = ManualClock()
    engine = ScenarioEngine(store, (Scenario("cycle", (SetAction("cpu", 2, 90),), 10),), clock)
    engine.start()
    clock.value = 2
    engine.tick()
    metric_logs.clear()
    clock.value = 10

    engine.tick()

    assert metric_logs.messages == [f"metric cpu oid={OID} changed: 90 -> 10 (scenario=cycle)"]


def test_each_changed_metric_has_its_own_values_oid_and_scenario(metric_logs) -> None:
    status_oid = "1.3.6.1.4.1.55555.2.0"
    store = MetricStore(
        [
            Metric("cpu", ObjectIdentifier.parse(OID), SnmpDataType.INTEGER32, 10),
            Metric("status", ObjectIdentifier.parse(status_oid), SnmpDataType.OCTET_STRING, "down"),
        ]
    )
    engine = ScenarioEngine(
        store,
        (
            Scenario("load", (SetAction("cpu", 0, 30),)),
            Scenario("recovery", (SetAction("status", 0, "up"),)),
        ),
    )

    engine.tick()

    assert metric_logs.messages == [
        f"metric cpu oid={OID} changed: 10 -> 30 (scenario=load)",
        f"metric status oid={status_oid} changed: 'down' -> 'up' (scenario=recovery)",
    ]


def test_equivalent_normalized_value_is_not_reported_as_a_change(metric_logs) -> None:
    store = MetricStore(
        [Metric("reference", ObjectIdentifier.parse(OID), SnmpDataType.OBJECT_IDENTIFIER, OID)]
    )
    engine = ScenarioEngine(
        store,
        (Scenario("reference", (SetAction("reference", 0, ".1.03.6.1.4.1.55555.1.0"),)),),
    )

    engine.tick()

    assert store.get_by_name("reference").value == OID
    assert metric_logs.records == []


@pytest.mark.parametrize(
    "device,scenario,timestamp,old_value,new_value,scenario_name",
    [
        ("device.json", "cpu-step.json", 1, 15, 25, "json"),
        ("device.yaml", None, 8, 15, 90, "cpu_demo"),
    ],
)
def test_json_and_yaml_use_the_same_metric_logging(
    metric_logs, device, scenario, timestamp, old_value, new_value, scenario_name
) -> None:
    config = (
        load_json_config(EXAMPLES / device, EXAMPLES / scenario)
        if scenario is not None
        else load_config(EXAMPLES / device)
    )
    clock = ManualClock()
    engine = ScenarioEngine(
        build_store(config),
        build_scenarios(config.scenarios),
        clock,
        tick_interval=config.update_interval,
        duration=config.duration,
    )
    engine.start()
    clock.value = timestamp

    engine.tick()

    assert metric_logs.messages == [
        "metric cpu_usage oid=1.3.6.1.4.1.55555.1.1.0 changed: "
        f"{old_value} -> {new_value} (scenario={scenario_name})"
    ]


def test_step_limit_and_completed_duration_do_not_repeat_metric_logs(metric_logs) -> None:
    store = numeric_store()
    clock = ManualClock()
    engine = ScenarioEngine(
        store,
        (Scenario("bounded", (StepAction("cpu", 0, 1, 10, 10, maximum=20),)),),
        clock,
        tick_interval=1,
        duration=2,
    )
    engine.start()
    engine.tick()
    assert metric_logs.records == []
    clock.value = 1
    assert engine.tick() is False
    assert metric_logs.messages == [f"metric cpu oid={OID} changed: 10 -> 20 (scenario=bounded)"]
    metric_logs.clear()

    for timestamp in (2, 3, 100):
        clock.value = timestamp
        assert engine.tick() is True

    assert store.get_by_name("cpu").value == 20
    assert metric_logs.records == []


def test_new_engine_and_store_start_a_fresh_logging_session(metric_logs) -> None:
    scenarios = (Scenario("load", (SetAction("cpu", 0, 20),)),)
    for _ in range(2):
        engine = ScenarioEngine(numeric_store(), scenarios, ManualClock())
        metric_logs.clear()
        engine.tick()
        assert metric_logs.messages == [f"metric cpu oid={OID} changed: 10 -> 20 (scenario=load)"]
