from dataclasses import dataclass

from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType
from snmp_emulator.scenarios.actions import SequenceAction, SetAction
from snmp_emulator.scenarios.engine import Scenario, ScenarioEngine


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
