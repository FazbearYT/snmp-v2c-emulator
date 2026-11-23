"""Structured JSON reports for completed, stopped and failed SNMP runs."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path

import pytest
from pyasn1.codec.ber import encoder
from pysnmp.proto import rfc1902
from pysnmp.proto.api import v2c

from snmp_emulator.application import EmulatorApplication
from snmp_emulator.json_config import DeviceConfig, ScenarioDocument, bind_config
from snmp_emulator.run_report import (
    RequestCounters,
    RunStatistics,
    build_report,
    save_report,
    utc_now,
)

OID = "1.3.6.1.4.1.55555.1.0"


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def configuration(*, port: int | None = None, duration: float = 0.3):
    device = DeviceConfig.model_validate(
        {
            "agent": {"host": "127.0.0.1", "port": port or reserve_port(),
                      "community": "super-secret-value"},
            "metrics": [{"name": "cpu", "oid": OID, "type": "Integer32", "initial": 15}],
        }
    )
    scenario = ScenarioDocument.model_validate(
        {
            "duration": duration,
            "update_interval": 0.05,
            "rules": [{"type": "constant", "oid": OID, "value": 15}],
        }
    )
    return bind_config(device, scenario)


def snmp_get() -> bytes:
    pdu = v2c.GetRequestPDU()
    v2c.apiPDU.set_defaults(pdu)
    v2c.apiPDU.set_varbinds(pdu, [(rfc1902.ObjectName(OID), rfc1902.Null(""))])
    message = v2c.Message()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, b"super-secret-value")
    v2c.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


async def get_response(port: int) -> bytes:
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        await loop.sock_sendto(sock, snmp_get(), ("127.0.0.1", port))
        return await asyncio.wait_for(loop.sock_recv(sock, 65535), 1)


async def terminate(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_report_after_scenario_completion_and_stop(tmp_path: Path):
    path = tmp_path / "results" / "completed.json"
    config = configuration(duration=0.2)
    application = EmulatorApplication(
        config, scenario_id="stable", report_path=path
    )
    task = asyncio.create_task(application.run())
    await asyncio.wait_for(application.started.wait(), 2)
    await get_response(config.agent.port)
    await asyncio.sleep(0.28)
    await terminate(task)

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["scenario_id"] == "stable"
    assert report["outcome"] == "completed"
    assert report["scenario_completed"]
    assert report["initial_metrics"] == {"cpu": 15}
    assert report["final_metrics"] == {"cpu": 15}
    assert report["requests"]["received"] == 1
    assert report["requests"]["sent"] == 1
    assert report["duration_seconds"] >= 0.2
    assert report["errors"] == []
    assert "super-secret-value" not in path.read_text(encoding="utf-8")
    assert "community" not in report["configuration"]["agent"]


@pytest.mark.asyncio
async def test_early_stop_is_distinct_from_completed_run(tmp_path: Path):
    config = configuration(duration=3.0)
    path = tmp_path / "stopped.json"
    application = EmulatorApplication(config, scenario_id="long", report_path=path)
    task = asyncio.create_task(application.run())
    await asyncio.wait_for(application.started.wait(), 2)
    await terminate(task)
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["outcome"] == "stopped_early"
    assert not report["scenario_completed"]
    assert not report["errors"]


@pytest.mark.asyncio
async def test_handled_bind_failure_saves_failed_report(tmp_path: Path):
    port = reserve_port()
    config = configuration(port=port)
    path = tmp_path / "failed.json"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
        busy.bind(("127.0.0.1", port))
        application = EmulatorApplication(
            config, scenario_id="failed", report_path=path
        )
        with pytest.raises(OSError):
            await application.run()
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["outcome"] == "failed"
    assert report["errors"]
    assert report["requests"]["received"] == 0
    assert "super-secret-value" not in path.read_text(encoding="utf-8")


def test_request_counters_do_not_store_request_history():
    counters = RequestCounters()
    for _ in range(6000):
        counters.received_request()
    counters.finished_request(
        outcome="not_sent",
        error_status=None,
        exceptions=(),
        reason="device_unavailable",
        simulated_generr=False,
    )
    counters.finished_request(
        outcome="sent",
        error_status=5,
        exceptions=(),
        reason=None,
        simulated_generr=True,
    )
    counters.finished_request(
        outcome="sent",
        error_status=0,
        exceptions=(f"{OID}:noSuchObject", f"{OID}:noSuchInstance"),
        reason=None,
        simulated_generr=True,
    )
    assert counters.received == 6000
    assert counters.intentionally_suppressed == 1
    assert counters.simulated_errors == 1
    assert counters.no_such_object == counters.no_such_instance == 1
    assert counters.sent == 2
    assert len(vars(counters)) == 7


def test_report_write_error_does_not_destroy_existing_file(tmp_path: Path, monkeypatch):
    import snmp_emulator.run_report as reports

    path = tmp_path / "report.json"
    path.write_text('{"previous":true}', encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(reports.json, "dump", fail)
    with pytest.raises(OSError, match="write failed"):
        save_report(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"previous": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_build_report_has_no_raw_log_and_limits_errors():
    config = configuration()
    stats = RunStatistics()
    for _ in range(100):
        stats.record_error("safe failure")
    now = utc_now()
    report = build_report(
        config, stats, scenario_id="sample", started=now, ended=now,
        initial_values={"cpu": 15}, final_values={"cpu": 15},
        scenario_completed=False, failed=True,
    )
    assert report["outcome"] == "failed"
    assert len(report["errors"]) == 32
    assert report["requests"]["received"] == 0
    assert "community" not in report["configuration"]["agent"]


@pytest.mark.asyncio
async def test_unavailable_requests_counted_without_responses(tmp_path: Path):
    config = configuration(duration=1.1)
    from snmp_emulator.config import DeviceUnavailabilityConfig

    config = config.model_copy(
        update={"unavailability": DeviceUnavailabilityConfig(at=0.1, duration=0.8)}
    )
    path = tmp_path / "unavailable.json"
    app = EmulatorApplication(config, scenario_id="outage", report_path=path)
    task = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(app.started.wait(), 2)
        async with asyncio.timeout(1):
            while not app.agent.unavailable:
                await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            await loop.sock_sendto(
                sock, snmp_get(), ("127.0.0.1", config.agent.port)
            )
        await asyncio.sleep(0.05)
    finally:
        await terminate(task)
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["requests"]["received"] == 1
    assert report["requests"]["intentionally_suppressed"] == 1


def test_report_persistence_emits_no_credentials(caplog, tmp_path):
    config = configuration()
    stats = RunStatistics()
    now = utc_now()
    document = build_report(
        config, stats, scenario_id="example", started=now, ended=now,
        initial_values={}, final_values={},
        scenario_completed=True, failed=False,
    )
    path = tmp_path / "output.json"
    with caplog.at_level(logging.INFO):
        save_report(path, document)
    assert "super-secret-value" not in path.read_text(encoding="utf-8")
