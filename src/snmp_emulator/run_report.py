"""Bounded, structured run statistics and atomic JSON persistence.

No request history, credentials or SNMP community are retained in the report.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import EmulatorConfig


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RequestCounters:
    received: int = 0
    sent: int = 0
    intentionally_suppressed: int = 0
    simulated_errors: int = 0
    no_such_object: int = 0
    no_such_instance: int = 0
    unsuccessful_delivery: int = 0

    def received_request(self) -> None:
        self.received += 1

    def finished_request(
        self,
        *,
        outcome: str,
        error_status: int | None,
        exceptions: tuple[str, ...],
        reason: str | None,
        simulated_generr: bool,
    ) -> None:
        if outcome == "sent":
            self.sent += 1
            if simulated_generr and error_status == 5:
                self.simulated_errors += 1
            self.no_such_object += sum(item.endswith(":noSuchObject") for item in exceptions)
            self.no_such_instance += sum(item.endswith(":noSuchInstance") for item in exceptions)
        elif reason == "device_unavailable":
            self.intentionally_suppressed += 1
        else:
            self.unsuccessful_delivery += 1


@dataclass
class RunStatistics:
    requests: RequestCounters = field(default_factory=RequestCounters)
    errors: list[str] = field(default_factory=list)
    _error_limit: int = 32

    def record_error(self, message: str) -> None:
        # Bound error history as well; no secrets should be included in messages.
        if len(self.errors) < self._error_limit:
            self.errors.append(message)


def public_config(config: EmulatorConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    data["agent"].pop("community", None)
    return data


def build_report(
    config: EmulatorConfig,
    stats: RunStatistics,
    *,
    scenario_id: str,
    started: datetime,
    ended: datetime,
    initial_values: dict[str, Any],
    final_values: dict[str, Any],
    scenario_completed: bool,
    failed: bool,
) -> dict[str, Any]:
    status = "failed" if failed else ("completed" if scenario_completed else "stopped_early")
    return {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "configuration": public_config(config),
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_seconds": max(0.0, (ended - started).total_seconds()),
        "scenario_completed": scenario_completed,
        "outcome": status,
        "initial_metrics": initial_values,
        "final_metrics": final_values,
        "requests": {
            "received": stats.requests.received,
            "sent": stats.requests.sent,
            "intentionally_suppressed": stats.requests.intentionally_suppressed,
            "simulated_errors": stats.requests.simulated_errors,
            "no_such_object": stats.requests.no_such_object,
            "no_such_instance": stats.requests.no_such_instance,
            "unsuccessful_delivery": stats.requests.unsuccessful_delivery,
        },
        "errors": list(stats.errors),
    }


def save_report(path: Path, document: dict[str, Any]) -> None:
    """Write to a temporary file and replace the destination atomically."""
    path = Path(path)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def default_report_path(source: Path) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    return Path.cwd() / "reports" / f"{source.stem}-{stamp}.json"
