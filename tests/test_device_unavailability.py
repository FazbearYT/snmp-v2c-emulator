"""Network and validation coverage for task 08 device unavailability."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

import pytest
from pydantic import ValidationError
from pysnmp.entity import engine
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    UdpTransportTarget,
    bulk_cmd,
    get_cmd,
    next_cmd,
)

from snmp_emulator.application import EmulatorApplication
from snmp_emulator.config import EmulatorConfig
from snmp_emulator.gui_controller import PanelController
from snmp_emulator.json_config import DeviceConfig, ScenarioDocument, bind_config

OID = "1.3.6.1.4.1.55555.1.0"
PREFIX = "1.3.6.1.4.1.55555"


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(*, at: float = 0.4, length: float = 0.8, total: float = 2.0):
    device = DeviceConfig.model_validate(
        {
            "agent": {"host": "127.0.0.1", "port": reserve_port(), "community": "private"},
            "metrics": [{"name": "cpu", "oid": OID, "type": "Integer32", "initial": 15}],
        }
    )
    scenario = ScenarioDocument.model_validate(
        {
            "duration": total,
            "update_interval": 0.05,
            "rules": [],
            "unavailability": {"at": at, "duration": length},
        }
    )
    return bind_config(device, scenario)


async def until(predicate, *, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def query(client, target, kind):
    args = (client, CommunityData("private", mpModel=1), target, ContextData())
    oid = OID if kind == "GET" else PREFIX
    binding = ObjectType(ObjectIdentity(oid))
    if kind == "GET":
        return await get_cmd(*args, binding)
    if kind == "GETNEXT":
        return await next_cmd(*args, binding)
    return await bulk_cmd(*args, 0, 1, binding)


@pytest.mark.asyncio
async def test_device_responds_before_and_after_outage_but_not_during(caplog):
    config = make_config(at=0.5, length=1.0, total=2.5)
    app = EmulatorApplication(config)
    client = engine.SnmpEngine()
    caplog.set_level(logging.INFO, logger="snmp_emulator.adapters.request_audit")
    caplog.set_level(logging.INFO, logger="snmp_emulator.adapters.pysnmp_agent")
    task = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(app.started.wait(), 2)
        target = await UdpTransportTarget.create(
            ("127.0.0.1", config.agent.port), timeout=0.12, retries=0
        )
        error, status, _, bindings = await query(client, target, "GET")
        assert error is None and not status
        assert int(bindings[0][1]) == 15

        await until(lambda: app.agent.unavailable)
        for kind in ("GET", "GETNEXT", "GETBULK"):
            error, status, _, bindings = await query(client, target, kind)
            assert error is not None and not status and not bindings

        await until(lambda: not app.agent.unavailable)
        error, status, _, bindings = await query(client, target, "GET")
        assert error is None and not status
        assert int(bindings[0][1]) == 15
        lines = [r.getMessage() for r in caplog.records]
        suppressed = [line for line in lines if "reason=device_unavailable" in line]
        assert len(suppressed) == 3
        assert all("outcome=not_sent" in line for line in suppressed)
        assert any("pdu=GETBULK" in line for line in lines)
        assert any("pdu=GETNEXT" in line for line in lines)
        assert any("SNMP availability changed: unavailable" in line for line in lines)
        assert any("SNMP availability changed: available" in line for line in lines)
        assert all("private" not in line for line in lines)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.close_dispatcher()


@pytest.mark.asyncio
async def test_cancel_and_restart_clears_unavailable_state():
    config = make_config(at=0.1, length=0.6, total=1.0)
    app = EmulatorApplication(config)
    for _ in range(2):
        task = asyncio.create_task(app.run())
        try:
            await asyncio.wait_for(app.started.wait(), 2)
            await until(lambda: app.agent.unavailable)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not app.agent.unavailable
        assert not app.started.is_set()
        assert app.store.get_by_name("cpu").value == 15


@pytest.mark.parametrize(
    "outage",
    [
        {"at": -0.01, "duration": 0.1},
        {"at": 0.1, "duration": 0},
        {"at": 0.9, "duration": 0.2},
        {"at": True, "duration": 0.2},
    ],
)
def test_rejects_invalid_unavailability(outage):
    with pytest.raises(ValidationError):
        ScenarioDocument.model_validate(
            {
                "duration": 1.0,
                "update_interval": 0.1,
                "rules": [],
                "unavailability": outage,
            }
        )


def test_requires_constant_values_and_validates_legacy_yaml_model():
    with pytest.raises(ValidationError, match="constant"):
        ScenarioDocument.model_validate(
            {
                "duration": 2.0,
                "update_interval": 0.1,
                "rules": [
                    {"type": "jump", "oid": OID, "at": 1.0, "value": 16}
                ],
                "unavailability": {"at": 0.5, "duration": 0.5},
            }
        )
    with pytest.raises(ValidationError, match="constant"):
        EmulatorConfig.model_validate(
            {
                "schema_version": 1,
                "metrics": [{"name": "cpu", "oid": OID, "type": "Integer32", "initial": 15}],
                "duration": 2.0,
                "unavailability": {"at": 0.5, "duration": 0.5},
                "scenarios": [
                    {
                        "name": "legacy",
                        "actions": [
                            {"type": "set", "metric": "cpu", "at": 1.0, "value": 16}
                        ],
                    }
                ],
            }
        )


def test_outage_only_scenario_has_no_metric_actions():
    config = make_config()
    assert config.scenarios[0].actions == []
    assert config.unavailability is not None
    assert config.unavailability.at == 0.4
    assert config.unavailability.duration == 0.8


def test_gui_panel_displays_temporary_unavailability(tmp_path):
    from snmp_emulator.gui_controller import PROFILES

    assert any("Недоступность" in name for name in PROFILES)

    port = reserve_port()
    device = tmp_path / "device.json"
    scenario = tmp_path / "outage.json"
    device.write_text(
        json.dumps(
            {
                "agent": {"host": "127.0.0.1", "port": port},
                "metrics": [
                    {"name": "cpu", "oid": OID, "type": "Integer32", "initial": 15}
                ],
            }
        ),
        encoding="utf-8",
    )
    scenario.write_text(
        json.dumps(
            {
                "duration": 1.0,
                "update_interval": 0.05,
                "rules": [],
                "unavailability": {"at": 0.2, "duration": 0.5},
            }
        ),
        encoding="utf-8",
    )
    panel = PanelController()
    assert panel.start(device, scenario)
    seen = []
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = panel.snapshot().state
            if state not in seen:
                seen.append(state)
            if "unavailable" in seen and state == "running":
                break
            time.sleep(0.01)
        assert "unavailable" in seen
        assert "running" in seen
        assert panel.snapshot().metrics[0].value == 15
    finally:
        panel.stop()
        deadline = time.monotonic() + 4
        while panel.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not panel.active
