import asyncio
import contextlib
import socket
from pathlib import Path

import pytest
from installed_smoke import reserve_udp_port

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


@pytest.mark.asyncio
async def test_duplicate_run_does_not_stop_running_application() -> None:
    config = load_config(EXAMPLE_CONFIG)
    config = config.model_copy(
        update={"agent": config.agent.model_copy(update={"port": reserve_udp_port()})}
    )
    application = EmulatorApplication(config)
    task = asyncio.create_task(application.run())
    try:
        await asyncio.wait_for(application.started.wait(), 3)
        with pytest.raises(RuntimeError, match="already running"):
            await application.run()
        assert application.started.is_set()
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_duplicate_run_during_start_does_not_clean_up_first_start() -> None:
    application = EmulatorApplication(load_config(EXAMPLE_CONFIG))

    class PausedAgent:
        def __init__(self):
            self.entered = asyncio.Event()
            self.stopped = False

        async def start(self):
            self.entered.set()
            await asyncio.Event().wait()

        async def stop(self):
            self.stopped = True

    application.agent = PausedAgent()
    task = asyncio.create_task(application.run())
    try:
        await asyncio.wait_for(application.agent.entered.wait(), 3)
        with pytest.raises(RuntimeError, match="already running"):
            await application.run()
        assert not application.agent.stopped
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert application.agent.stopped


@pytest.mark.asyncio
async def test_application_can_retry_after_failed_bind() -> None:
    config = load_config(EXAMPLE_CONFIG)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        config = config.model_copy(
            update={"agent": config.agent.model_copy(update={"port": occupied.getsockname()[1]})}
        )
        application = EmulatorApplication(config)
        with pytest.raises(OSError, match="cannot bind"):
            await application.run()
    task = asyncio.create_task(application.run())
    try:
        await asyncio.wait_for(application.started.wait(), 3)
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
