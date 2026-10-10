import asyncio
from pathlib import Path

import pytest

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


@pytest.mark.asyncio
async def test_stops_agent_when_scenario_engine_fails() -> None:
    config = load_config(EXAMPLE_CONFIG)
    application = EmulatorApplication(config)

    class FakeAgent:
        started = False
        stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    class FailingScenarioEngine:
        async def run(self) -> None:
            raise RuntimeError("scenario failed")

    agent = FakeAgent()
    application.agent = agent
    application.scenario_engine = FailingScenarioEngine()

    with pytest.raises(ExceptionGroup) as error:
        await application.run()

    assert isinstance(error.value.exceptions[0], RuntimeError)
    assert str(error.value.exceptions[0]) == "scenario failed"
    assert agent.started
    assert agent.stopped


@pytest.mark.asyncio
async def test_cancellation_during_start_still_stops_agent() -> None:
    config = load_config(EXAMPLE_CONFIG)
    application = EmulatorApplication(config)

    class HangingAgent:
        def __init__(self) -> None:
            self.start_entered = asyncio.Event()
            self.stopped = False

        async def start(self) -> None:
            self.start_entered.set()
            await asyncio.Event().wait()

        async def stop(self) -> None:
            self.stopped = True

    agent = HangingAgent()
    application.agent = agent
    task = asyncio.create_task(application.run())
    await agent.start_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.stopped
