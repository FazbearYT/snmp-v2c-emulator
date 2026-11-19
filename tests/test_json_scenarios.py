import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from snmp_emulator.application import EmulatorApplication, build_store
from snmp_emulator.json_config import DeviceConfig, ScenarioDocument, bind_config, load_json_config
from snmp_emulator.scenarios.engine import ScenarioEngine, build_scenarios

OID = "1.3.6.1.4.1.55555.1.0"
EXAMPLES = Path(__file__).parents[1] / "examples"


@dataclass
class ManualClock:
    value: Decimal = Decimal(0)

    def now(self) -> Decimal:
        return self.value


def test_recovery_starts_independently_and_preserves_accumulated_errors() -> None:
    config = load_json_config(EXAMPLES / "device-emergency.json", EXAMPLES / "recovery.json")
    normal = DeviceConfig.model_validate_json((EXAMPLES / "device-normal.json").read_text())
    assert [(m.name, m.oid, m.type) for m in config.metrics] == [
        (m.name, m.oid, m.type) for m in normal.metrics
    ]
    assert config.agent == normal.agent
    initial = {m.name: m.initial for m in config.metrics}
    assert initial == {
        "cpu_usage": 95,
        "temperature": 80,
        "description": "SNMP demo device",
        "memory_usage": 40,
        "interface_status": "down",
        "interface_errors": 120,
    }
    expected_final = {m.name: m.initial for m in normal.metrics}
    expected_final["interface_errors"] = 120
    for _ in range(2):
        application = EmulatorApplication(config)
        clock = ManualClock()
        scenario = ScenarioEngine(
            application.store,
            build_scenarios(config.scenarios),
            clock,
            tick_interval=config.update_interval,
            duration=config.duration,
        )
        scenario.start()
        for timestamp, cpu, temperature, status in (
            ("0", 95, 80, "down"),
            ("0.999", 95, 80, "down"),
            ("1", 85, 75, "down"),
            ("8", 15, 40, "down"),
            ("9", 15, 35, "down"),
            ("10", 15, 35, "up"),
            ("12", 15, 35, "up"),
            ("100", 15, 35, "up"),
        ):
            clock.value = Decimal(timestamp)
            scenario.tick()
            values = {m.name: m.value for m in application.store.snapshot()}
            assert values == {
                **initial,
                "cpu_usage": cpu,
                "temperature": temperature,
                "interface_status": status,
            }
        assert values == expected_final
    assert {m.name: m.initial for m in config.metrics} == initial


def configuration(rule: dict, duration: float = 1, interval: float = 0.1):
    return bind_config(
        DeviceConfig.model_validate(
            {
                "agent": {},
                "metrics": [{"name": "cpu", "oid": OID, "type": "Integer32", "initial": 10}],
            }
        ),
        ScenarioDocument.model_validate(
            {"duration": duration, "update_interval": interval, "rules": [{"oid": OID, **rule}]}
        ),
    )


@pytest.mark.parametrize(
    "rule,checks",
    [
        ({"type": "constant", "value": 7}, [("0", 7), ("0.9", 7), ("2", 7)]),
        (
            {"type": "jump", "at": 0.25, "value": 80},
            [("0", 10), ("0.25", 10), ("0.299", 10), ("0.3", 80), ("2", 80)],
        ),
        (
            {"type": "step", "step": 3, "limit": 20},
            [("0", 10), ("0.099", 10), ("0.1", 13), ("0.3", 19), ("0.4", 20), ("2", 20)],
        ),
        (
            {"type": "step", "step": -3, "limit": -1},
            [("0", 10), ("0.1", 7), ("0.3", 1), ("0.4", -1), ("2", -1)],
        ),
    ],
)
def test_rules_follow_update_grid_and_limits(rule: dict, checks: list) -> None:
    config = configuration(rule)
    store = build_store(config)
    clock = ManualClock()
    engine = ScenarioEngine(
        store,
        build_scenarios(config.scenarios),
        clock,
        tick_interval=config.update_interval,
        duration=config.duration,
    )
    engine.start()
    for timestamp, expected in checks:
        clock.value = Decimal(timestamp)
        engine.tick()
        assert store.get_by_name("cpu").value == expected


@pytest.mark.parametrize("duration,expected", [(0.35, 13), (0.3, 13), (0.09, 10)])
def test_duration_freezes_last_scheduled_value(duration: float, expected: int) -> None:
    config = configuration({"type": "step", "step": 1, "limit": 100}, duration)
    store = build_store(config)
    clock = ManualClock()
    engine = ScenarioEngine(
        store,
        build_scenarios(config.scenarios),
        clock,
        tick_interval=config.update_interval,
        duration=config.duration,
    )
    engine.start()
    for timestamp in ("1", "10", "1000"):
        clock.value = Decimal(timestamp)
        engine.tick()
        assert store.get_by_name("cpu").value == expected


def test_jump_at_duration_and_large_fractional_tick_counts() -> None:
    config = configuration({"type": "jump", "at": 0.3, "value": 42}, 0.3)
    clock = ManualClock()
    store = build_store(config)
    engine = ScenarioEngine(
        store, build_scenarios(config.scenarios), clock, tick_interval=0.1, duration=0.3
    )
    engine.start()
    clock.value = Decimal("0.3")
    engine.tick()
    assert store.get_by_name("cpu").value == 42

    config = configuration({"type": "step", "step": 1, "limit": 2**31 - 1}, 100000)
    store = build_store(config)
    clock.value = Decimal(0)
    engine = ScenarioEngine(
        store, build_scenarios(config.scenarios), clock, tick_interval=0.1, duration=config.duration
    )
    engine.start()
    clock.value = Decimal("99999.9")
    engine.tick()
    assert store.get_by_name("cpu").value == 1000009


def test_new_application_restores_initial_values() -> None:
    config = configuration({"type": "step", "step": 10, "limit": 50})
    first = EmulatorApplication(config)
    first.store.update("cpu", 50)
    second = EmulatorApplication(config)
    assert second.store.get_by_name("cpu").value == 10
    assert config.metrics[0].initial == 10


@pytest.mark.asyncio
async def test_run_uses_absolute_deadlines_and_stops_at_duration(monkeypatch) -> None:
    config = configuration({"type": "step", "step": 1, "limit": 100}, 0.35)
    clock = ManualClock()
    store = build_store(config)
    engine = ScenarioEngine(
        store, build_scenarios(config.scenarios), clock, tick_interval=0.1, duration=0.35
    )
    delays = []

    async def delayed_wakeup(delay: float) -> None:
        delays.append(delay)
        clock.value += Decimal(str(delay)) + Decimal("0.03")

    monkeypatch.setattr(asyncio, "sleep", delayed_wakeup)
    await engine.run()
    assert delays == pytest.approx([0.1, 0.07, 0.07, 0.02])
    assert store.get_by_name("cpu").value == 13


@pytest.mark.asyncio
async def test_crossing_duration_during_tick_does_not_skip_final_value(monkeypatch) -> None:
    config = configuration({"type": "step", "step": 1, "limit": 100}, 0.3)
    readings = iter(map(Decimal, ("0", "0.299", "0.301", "0.302", "0.303")))

    class AdvancingClock:
        def now(self):
            return next(readings)

    store = build_store(config)
    engine = ScenarioEngine(
        store, build_scenarios(config.scenarios), AdvancingClock(), tick_interval=0.1, duration=0.3
    )
    delays = []

    async def wakeup(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", wakeup)
    await engine.run()
    assert store.get_by_name("cpu").value == 13
    assert delays == [0.0]
