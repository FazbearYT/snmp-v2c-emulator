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
