from __future__ import annotations

import asyncio
import contextlib
import logging
import sysconfig
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .application import EmulatorApplication
from .config import ConfigurationError
from .domain.metric import Metric
from .json_config import load_json_config
from .run_report import default_report_path

LOGGER = logging.getLogger(__name__)

PROFILES = {
    "Демо ТЗ · 1/5 · Нормальная работа": ("device-normal.json", "demo-normal.json"),
    "Демо ТЗ · 2/5 · Деградация": ("device-normal.json", "demo-degradation.json"),
    "Демо ТЗ · 3/5 · Восстановление": ("device-emergency.json", "recovery.json"),
    "Демо ТЗ · 4/5 · Полная недоступность": (
        "device-normal.json",
        "device-unavailability.json",
    ),
    "Демо ТЗ · 5/5 · Частичная недоступность": (
        "device-normal.json",
        "partial-oid-unavailability.json",
    ),
    "device · 5 метрик · Стабильные показатели": ("device.json", "stable.json"),
    "device · 5 метрик · Скачок температуры": ("device.json", "temperature-jump.json"),
    "device · 5 метрик · Рост загрузки процессора": ("device.json", "cpu-step.json"),
    "device · 7 метрик · Рост трафика": ("device-7.json", "traffic-growth.json"),
    "device · 8 метрик · Рост нагрузки": ("device-8.json", "load-growth.json"),
    "device · 6 метрик · Восстановление": ("device-emergency.json", "recovery.json"),
    "device · 6 метрик · Недоступность": (
        "device-normal.json",
        "device-unavailability.json",
    ),
    "device · 6 метрик · Частичная недоступность": (
        "device-normal.json",
        "partial-oid-unavailability.json",
    ),
}


def examples_directory() -> Path:
    installed = Path(sysconfig.get_path("data")) / "share" / "snmp-v2c-emulator" / "examples"
    source = Path(__file__).resolve().parents[2] / "examples"
    return source if (source / "device.json").is_file() else installed


@dataclass(frozen=True)
class PanelSnapshot:
    state: str = "idle"
    metrics: tuple[Metric, ...] = ()
    elapsed: float = 0.0
    duration: float | None = None
    address: str = ""
    community: str = ""
    error: str = ""
    session: int = 0
    profile: str = ""


class PanelController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = PanelSnapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session = 0

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> PanelSnapshot:
        with self._lock:
            return self._snapshot

    def _publish(self, **changes: object) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)

    def start(self, device: Path, scenario: Path) -> bool:
        if self.active:
            return False
        self._stop.clear()
        self._session += 1
        with self._lock:
            self._snapshot = PanelSnapshot(
                state="starting", session=self._session, profile=f"{device.name} + {scenario.name}"
            )
        self._thread = threading.Thread(target=self._run, args=(device, scenario))
        self._thread.start()
        return True

    def stop(self) -> None:
        if self.active:
            self._stop.set()

    def _run(self, device: Path, scenario: Path) -> None:
        try:
            config = load_json_config(device, scenario)
            if not self._stop.is_set():
                asyncio.run(
                    self._observe(
                        EmulatorApplication(
                            config,
                            scenario_id=scenario.stem,
                            report_path=default_report_path(scenario),
                        )
                    )
                )
        except ConfigurationError as exc:
            LOGGER.error("configuration error: %s", exc)
            self._publish(state="error", error=str(exc))
        except OSError as exc:
            LOGGER.error("emulator failed: %s", exc)
            self._publish(state="error", error=str(exc))
        except Exception as exc:
            LOGGER.exception("emulator stopped unexpectedly")
            self._publish(state="error", error=str(exc))
        else:
            self._publish(state="stopped")

    async def _observe(self, application: EmulatorApplication) -> None:
        task = asyncio.create_task(application.run())
        ready = asyncio.create_task(application.started.wait())
        try:
            while not ready.done():
                if self._stop.is_set():
                    return
                if task.done():
                    await task
                    raise RuntimeError("Эмулятор завершился до запуска")
                await asyncio.wait((task, ready), timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
            started_at = time.monotonic()
            config = application.config
            while not self._stop.is_set():
                if task.done():
                    await task
                    raise RuntimeError("Эмулятор неожиданно завершился")
                elapsed = time.monotonic() - started_at
                completed = config.duration is not None and elapsed >= config.duration
                self._publish(
                    state=(
                        "unavailable"
                        if application.agent.unavailable
                        else ("completed" if completed else "running")
                    ),
                    elapsed=elapsed,
                    duration=config.duration,
                    address=f"{config.agent.host}:{config.agent.port}",
                    community=config.agent.community,
                    metrics=application.store.snapshot(),
                )
                await asyncio.sleep(0.1)
        finally:
            ready.cancel()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready
            with contextlib.suppress(asyncio.CancelledError):
                await task
