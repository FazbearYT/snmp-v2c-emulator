from __future__ import annotations

import asyncio
import socket

from pysnmp.entity import engine
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    UdpTransportTarget,
    get_cmd,
)

from snmp_emulator.adapters.pysnmp_agent import SnmpAgent
from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType


def reserve_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def smoke_installed_agent() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("smoke", oid, SnmpDataType.INTEGER, 42)])
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    client_engine = engine.SnmpEngine()
    await agent.start()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
        error, status, _, var_binds = await get_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )
        if error is not None or status or len(var_binds) != 1 or int(var_binds[0][1]) != 42:
            raise RuntimeError(
                "installed wheel did not answer the SNMP v2c smoke request: "
                f"error={error!r}, status={status!r}, var_binds={var_binds!r}"
            )
    finally:
        client_engine.close_dispatcher()
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(smoke_installed_agent())
