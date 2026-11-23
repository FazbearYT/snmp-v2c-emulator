from __future__ import annotations

import asyncio
import logging

from .adapters.pysnmp_agent import SnmpAgent
from .config import EmulatorConfig
from .domain.metric import Metric
from .domain.oid import ObjectIdentifier
from .domain.store import MetricStore
from .scenarios.engine import ScenarioEngine, build_scenarios

LOGGER = logging.getLogger(__name__)


def build_store(config: EmulatorConfig) -> MetricStore:
    return MetricStore(
        Metric(
            name=item.name,
            oid=ObjectIdentifier.parse(item.oid),
            data_type=item.type,
            value=item.initial,
        )
        for item in config.metrics
    )


class EmulatorApplication:
    def __init__(self, config: EmulatorConfig) -> None:
        self.config = config
        self.started = asyncio.Event()
        self._running = False
        self.store = build_store(config)
        self.agent = SnmpAgent(
            config.agent.host,
            config.agent.port,
            config.agent.community,
            self.store,
        )
        self.scenario_engine = ScenarioEngine(
            self.store,
            build_scenarios(config.scenarios),
            tick_interval=config.update_interval,
            duration=config.duration,
        )

    async def _schedule_unavailability(self, started_at: float) -> None:
        outage = self.config.unavailability
        if outage is None:
            return
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0, started_at + outage.at - loop.time()))
        self.agent.set_unavailable(True)
        try:
            await asyncio.sleep(max(0, started_at + outage.at + outage.duration - loop.time()))
        finally:
            self.agent.set_unavailable(False)

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("emulator application is already running")
        self._running = True
        try:
            await self.agent.start()
            started_at = asyncio.get_running_loop().time()
            self.started.set()
            LOGGER.info("listening on udp://%s:%d", self.config.agent.host, self.config.agent.port)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.scenario_engine.run())
                if self.config.unavailability is not None:
                    tasks.create_task(self._schedule_unavailability(started_at))
                tasks.create_task(asyncio.Event().wait())
        finally:
            was_started = self.started.is_set()
            self.started.clear()
            try:
                await self.agent.stop()
                if was_started:
                    LOGGER.info("emulator stopped")
            finally:
                self._running = False
