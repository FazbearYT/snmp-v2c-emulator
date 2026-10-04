from __future__ import annotations

import asyncio
from contextlib import suppress

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
        self.store = build_store(config)
        self.agent = SnmpAgent(
            config.agent.host,
            config.agent.port,
            config.agent.community,
            self.store,
        )
        self.scenario_engine = ScenarioEngine(self.store, build_scenarios(config.scenarios))

    async def run(self) -> None:
        await self.agent.start()
        scenario_task = asyncio.create_task(self.scenario_engine.run())
        try:
            await asyncio.Event().wait()
        finally:
            scenario_task.cancel()
            with suppress(asyncio.CancelledError):
                await scenario_task
            await self.agent.stop()
