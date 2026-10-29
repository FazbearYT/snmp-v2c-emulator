from __future__ import annotations

import asyncio

from .adapters.pysnmp_agent import SnmpAgent
from .config import EmulatorConfig
from .domain.metric import Metric
from .domain.oid import ObjectIdentifier
from .domain.store import MetricStore
from .scenarios.engine import ScenarioEngine, build_scenarios


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

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("emulator application is already running")
        self._running = True
        try:
            await self.agent.start()
            self.started.set()
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.scenario_engine.run())
                tasks.create_task(asyncio.Event().wait())
        finally:
            self.started.clear()
            try:
                await self.agent.stop()
            finally:
                self._running = False
