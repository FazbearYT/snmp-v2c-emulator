import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from installed_smoke import JSON_SCENARIOS, reserve_udp_port, smoke_json_scenario
from pysnmp.entity import engine
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    UdpTransportTarget,
    get_cmd,
)
from pysnmp.proto import rfc1902

from snmp_emulator.application import EmulatorApplication
from snmp_emulator.json_config import DeviceConfig, ScenarioDocument, bind_config


@pytest.mark.asyncio
@pytest.mark.parametrize("name", JSON_SCENARIOS)
async def test_json_examples_over_udp(name):
    await smoke_json_scenario(Path(__file__).parents[1] / "examples", name)


@pytest.mark.asyncio
async def test_recovery_cli_initial_final_and_restart_over_udp(tmp_path):
    examples = Path(__file__).parents[1] / "examples"
    device = json.loads((examples / "device-emergency.json").read_text(encoding="utf-8"))
    port = reserve_udp_port()
    device["agent"]["port"] = port
    device_path = tmp_path / "device.json"
    device_path.write_text(json.dumps(device), encoding="utf-8")
    initial = [metric["initial"] for metric in device["metrics"]]
    client = engine.SnmpEngine()
    target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=0.2, retries=0)

    async def values():
        error, status, _, bindings = await get_cmd(
            client,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            *(ObjectType(ObjectIdentity(metric["oid"])) for metric in device["metrics"]),
        )
        if error:
            return None
        assert not status
        result = []
        for metric, (_, value) in zip(device["metrics"], bindings, strict=True):
            if metric["type"] == "OctetString":
                assert isinstance(value, rfc1902.OctetString)
                result.append(value.asOctets().decode("utf-8"))
            else:
                assert isinstance(value, rfc1902.Integer32)
                result.append(int(value))
        return result

    try:
        for completed in (True, False):
            with (tmp_path / "cli.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "snmp_emulator",
                        "--device-config",
                        str(device_path),
                        "--scenario",
                        str(examples / "recovery.json"),
                    ],
                    stdout=log,
                    stderr=log,
                )
                try:
                    async with asyncio.timeout(5):
                        while "listening on" not in (tmp_path / "cli.log").read_text(
                            encoding="utf-8"
                        ):
                            assert process.poll() is None
                            await asyncio.sleep(0.01)
                        while (actual := await values()) is None:
                            assert process.poll() is None
                    assert actual == initial
                    if completed:
                        await asyncio.sleep(12.1)
                        assert process.poll() is None
                        assert await values() == [15, 35, "SNMP demo device", 40, "up", 120]
                finally:
                    process.terminate()
                    await asyncio.to_thread(process.communicate, timeout=5)
    finally:
        client.close_dispatcher()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [-(2**31), 2**31 - 1])
async def test_integer32_boundaries_have_correct_wire_type(value):
    oid = "1.3.6.1.4.1.55555.1.0"
    config = bind_config(
        DeviceConfig.model_validate(
            {
                "agent": {"host": "127.0.0.1", "port": reserve_udp_port()},
                "metrics": [
                    {"name": "boundary", "oid": oid, "type": "Integer32", "initial": value}
                ],
            }
        ),
        ScenarioDocument.model_validate(
            {
                "duration": 1,
                "update_interval": 1,
                "rules": [{"type": "constant", "oid": oid, "value": value}],
            }
        ),
    )
    app = EmulatorApplication(config)
    client = engine.SnmpEngine()
    await app.agent.start()
    try:
        target = await UdpTransportTarget.create(
            ("127.0.0.1", config.agent.port), timeout=1, retries=0
        )
        error, status, _, bindings = await get_cmd(
            client,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        assert error is None
        assert not status
        assert isinstance(bindings[0][1], rfc1902.Integer32)
        assert int(bindings[0][1]) == value
    finally:
        client.close_dispatcher()
        await app.agent.stop()
