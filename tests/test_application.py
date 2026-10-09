from pathlib import Path

from snmp_emulator.application import EmulatorApplication, build_store
from snmp_emulator.config import load_config

EXAMPLE_CONFIG = Path(__file__).parents[1] / "examples" / "device.yaml"


def test_builds_store_from_configuration() -> None:
    config = load_config(EXAMPLE_CONFIG)

    store = build_store(config)

    assert len(store) == 2
    assert store.get_by_name("cpu_usage").value == 15


def test_builds_application_components() -> None:
    config = load_config(EXAMPLE_CONFIG)

    application = EmulatorApplication(config)

    assert application.agent.store is application.store
    assert application.scenario_engine.store is application.store
    assert len(application.scenario_engine.scenarios) == 1
