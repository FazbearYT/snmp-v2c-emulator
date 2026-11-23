"""Contract tests for the five independent extended-specification demos."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest
from pyasn1.codec.ber import decoder, encoder
from pysnmp.proto import rfc1902
from pysnmp.proto.api import v2c
from pysnmp.smi import exval

from snmp_emulator.adapters.pysnmp_agent import V2cMessage
from snmp_emulator.application import EmulatorApplication
from snmp_emulator.config import EmulatorConfig
from snmp_emulator.gui_controller import PROFILES
from snmp_emulator.json_config import load_json_config

EXAMPLES = Path(__file__).parents[1] / "examples"
ROOT = "1.3.6.1.4.1.55555.4"
CPU, TEMP, DESC, MEMORY, STATUS, ERRORS = (
    f"{ROOT}.{i}.0" for i in range(1, 7)
)
PAIRS = {
    "normal": ("device-normal.json", "demo-normal.json"),
    "degradation": ("device-normal.json", "demo-degradation.json"),
    "recovery": ("device-emergency.json", "recovery.json"),
    "outage": ("device-normal.json", "device-unavailability.json"),
    "partial": ("device-normal.json", "partial-oid-unavailability.json"),
}
NORMAL = {CPU: 15, TEMP: 35, DESC: "SNMP demo device", MEMORY: 40, STATUS: "up", ERRORS: 0}
EMERGENCY = {
    CPU: 95, TEMP: 80, DESC: "SNMP demo device", MEMORY: 85,
    STATUS: "down", ERRORS: 120,
}
RECOVERED = {**NORMAL, ERRORS: 120}


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def scaled_config(name: str) -> EmulatorConfig:
    device, scenario = PAIRS[name]
    config = load_json_config(EXAMPLES / device, EXAMPLES / scenario)
    data = config.model_dump(mode="json")
    data["agent"]["port"] = reserve_port()
    # Accelerate real-time demonstrations without modifying the packaged JSON.
    data["duration"] /= 10
    data["update_interval"] /= 10
    for action in data["scenarios"][0]["actions"]:
        action["at"] /= 10
        if "interval" in action:
            action["interval"] /= 10
    if data.get("unavailability"):
        data["unavailability"]["at"] /= 10
        data["unavailability"]["duration"] /= 10
    return EmulatorConfig.model_validate(data)


def packet(oids: list[str], pdu_type=v2c.GetRequestPDU) -> bytes:
    pdu = pdu_type()
    v2c.apiPDU.set_defaults(pdu)
    v2c.apiPDU.set_request_id(pdu, 239)
    v2c.apiPDU.set_varbinds(
        pdu, [(rfc1902.ObjectName(oid), rfc1902.Null("")) for oid in oids]
    )
    message = V2cMessage()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, b"public")
    v2c.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


async def query(port: int, oids: list[str], *, timeout=1.0, pdu_type=v2c.GetRequestPDU):
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        await loop.sock_sendto(sock, packet(oids, pdu_type), ("127.0.0.1", port))
        data = await asyncio.wait_for(loop.sock_recv(sock, 65535), timeout)
    message, rest = decoder.decode(data, asn1Spec=V2cMessage())
    assert not rest
    return v2c.apiMessage.get_pdu(message)


def readings(pdu) -> dict[str, object]:
    values = {}
    for oid, value in v2c.apiPDU.get_varbinds(pdu):
        if value.tagSet in (
            exval.noSuchObject.tagSet, exval.noSuchInstance.tagSet, exval.endOfMibView.tagSet
        ):
            values[str(oid)] = value.tagSet
        elif str(oid) in (DESC, STATUS):
            values[str(oid)] = value.asOctets().decode("utf-8")
        else:
            values[str(oid)] = int(value)
    return values


async def wait_running(app: EmulatorApplication) -> None:
    await asyncio.wait_for(app.started.wait(), 3)
    async with asyncio.timeout(3):
        while app.scenario_engine._started_at is None:
            await asyncio.sleep(0.005)


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_all_five_demo_pairs_exist_validate_and_share_six_oid_types():
    reference = None
    for name, (device, scenario) in PAIRS.items():
        assert (EXAMPLES / device).is_file(), name
        assert (EXAMPLES / scenario).is_file(), name
        cfg = load_json_config(EXAMPLES / device, EXAMPLES / scenario)
        assert len(cfg.metrics) == 6
        signature = {(m.oid, m.type) for m in cfg.metrics}
        if reference is None:
            reference = signature
        assert signature == reference
    assert any("demo-normal.json" in pair for pair in PROFILES.values())
    assert any("demo-degradation.json" in pair for pair in PROFILES.values())
    for _, scenario in PAIRS.values():
        assert any(scenario in pair for pair in PROFILES.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_start", "expected_end"),
    [
        ("normal", NORMAL, NORMAL),
        ("degradation", NORMAL, EMERGENCY),
        ("recovery", EMERGENCY, RECOVERED),
    ],
)
async def test_demo_changes_are_visible_to_external_snmp_client(
    scenario: str, expected_start: dict[str, object], expected_end: dict[str, object]
):
    cfg = scaled_config(scenario)
    app = EmulatorApplication(cfg)
    task = asyncio.create_task(app.run())
    try:
        await wait_running(app)
        start = readings(await query(cfg.agent.port, list(NORMAL)))
        assert start == expected_start
        if scenario == "degradation":
            await asyncio.sleep(0.45)
            intermediate = readings(await query(cfg.agent.port, [CPU, MEMORY, TEMP, ERRORS]))
            assert 15 < intermediate[CPU] < 95
            assert 40 < intermediate[MEMORY] < 85
            assert 35 < intermediate[TEMP] < 80
            assert 0 < intermediate[ERRORS] < 120
        await asyncio.sleep(cfg.duration + 0.12)
        assert readings(await query(cfg.agent.port, list(NORMAL))) == expected_end
        # Once the scenario finishes, values remain fixed and requests still work.
        await asyncio.sleep(0.12)
        assert readings(await query(cfg.agent.port, list(NORMAL))) == expected_end
        assert not task.done()
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_full_outage_times_out_then_recovers_without_metric_changes():
    cfg = scaled_config("outage")
    app = EmulatorApplication(cfg)
    task = asyncio.create_task(app.run())
    try:
        await wait_running(app)
        assert readings(await query(cfg.agent.port, [CPU, MEMORY])) == {
            CPU: 15, MEMORY: 40
        }
        async with asyncio.timeout(2):
            while not app.agent.unavailable:
                await asyncio.sleep(0.005)
        with pytest.raises(TimeoutError):
            await query(cfg.agent.port, [CPU], timeout=0.09)
        async with asyncio.timeout(2):
            while app.agent.unavailable:
                await asyncio.sleep(0.005)
        assert readings(await query(cfg.agent.port, list(NORMAL))) == NORMAL
    finally:
        await stop(task)


@pytest.mark.asyncio
async def test_partial_outage_mixed_get_and_lexical_skip():
    cfg = scaled_config("partial")
    app = EmulatorApplication(cfg)
    task = asyncio.create_task(app.run())
    try:
        await wait_running(app)
        pdu = await query(cfg.agent.port, [CPU, TEMP, DESC, MEMORY, ERRORS])
        assert int(v2c.apiPDU.get_error_status(pdu)) == 0
        values = readings(pdu)
        assert values[CPU] == 15 and values[MEMORY] == 40 and values[ERRORS] == 0
        assert values[TEMP] == exval.noSuchObject.tagSet
        assert values[DESC] == exval.noSuchInstance.tagSet
        pdu = await query(cfg.agent.port, [CPU, STATUS, MEMORY])
        assert int(v2c.apiPDU.get_error_status(pdu)) == 5
        assert int(v2c.apiPDU.get_error_index(pdu)) == 2
        pdu = await query(cfg.agent.port, [CPU], pdu_type=v2c.GetNextRequestPDU)
        next_binding = v2c.apiPDU.get_varbinds(pdu)[0]
        assert str(next_binding[0]) == MEMORY
        assert int(next_binding[1]) == 40
    finally:
        await stop(task)


def test_emergency_config_matches_degradation_final_except_recovery_error_counter():
    first = json.loads((EXAMPLES / "device-normal.json").read_text(encoding="utf-8"))
    last = json.loads((EXAMPLES / "device-emergency.json").read_text(encoding="utf-8"))
    assert [m["oid"] for m in first["metrics"]] == [m["oid"] for m in last["metrics"]]
    assert [m["type"] for m in first["metrics"]] == [m["type"] for m in last["metrics"]]
    assert {m["oid"]: m["initial"] for m in last["metrics"]} == EMERGENCY
