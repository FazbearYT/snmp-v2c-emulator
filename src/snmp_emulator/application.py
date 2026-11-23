from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapters.pysnmp_agent import SnmpAgent
from .config import EmulatorConfig
from .domain.metric import Metric
from .domain.oid import ObjectIdentifier
from .domain.store import MetricStore
from .run_report import RunStatistics, build_report, save_report, utc_now
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
    def __init__(
        self,
        config: EmulatorConfig,
        *,
        scenario_id: str = "unspecified",
        report_path: Path | None = None,
    ) -> None:
        self.config = config
        self.scenario_id = scenario_id
        self.report_path = report_path
        self.report: dict[str, Any] | None = None
        self.stats = RunStatistics()
        self.scenario_completed = False
        self.started = asyncio.Event()
        self._running = False
        self.store = build_store(config)
        self.agent = SnmpAgent(
            config.agent.host,
            config.agent.port,
            config.agent.community,
            self.store,
            oid_responses={item.oid: item.mode for item in config.oid_responses},
            request_counters=self.stats.requests,
        )
        self.scenario_engine = ScenarioEngine(
            self.store,
            build_scenarios(config.scenarios),
            tick_interval=config.update_interval,
            duration=config.duration,
        )

    def configure_report(self, *, scenario_id: str, path: Path) -> None:
        self.scenario_id = scenario_id
        self.report_path = path

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

    async def _run_scenario(self) -> None:
        await self.scenario_engine.run()
        self.scenario_completed = True

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("emulator application is already running")
        self._running = True
        self.scenario_completed = False
        self.stats = RunStatistics()
        self.agent.request_counters = self.stats.requests
        self.report = None
        initial = {metric.name: metric.value for metric in self.store.snapshot()}
        started: datetime = utc_now()
        failed = False
        try:
            await self.agent.start()
            started_at = asyncio.get_running_loop().time()
            self.started.set()
            LOGGER.info("listening on udp://%s:%d", self.config.agent.host, self.config.agent.port)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._run_scenario())
                if self.config.unavailability is not None:
                    tasks.create_task(self._schedule_unavailability(started_at))
                tasks.create_task(asyncio.Event().wait())
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failed = True
            self.stats.record_error(type(exc).__name__)
            raise
        finally:
            was_started = self.started.is_set()
            self.started.clear()
            try:
                await self.agent.stop()
                if was_started:
                    LOGGER.info("emulator stopped")
            except Exception as exc:
                failed = True
                self.stats.record_error(type(exc).__name__)
                raise
            finally:
                ended = utc_now()
                final = {metric.name: metric.value for metric in self.store.snapshot()}
                self.report = build_report(
                    self.config,
                    self.stats,
                    scenario_id=self.scenario_id,
                    started=started,
                    ended=ended,
                    initial_values=initial,
                    final_values=final,
                    scenario_completed=self.scenario_completed,
                    failed=failed,
                )
                self._running = False
                if self.report_path is not None:
                    try:
                        save_report(self.report_path, self.report)
                        LOGGER.info("run report saved: %s", self.report_path)
                    except OSError as exc:
                        LOGGER.error("cannot save run report %s: %s", self.report_path, exc)
                        if not failed:
                            raise
