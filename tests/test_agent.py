import asyncio
import socket

import pytest
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
from pysnmp.proto import rfc1902

from snmp_emulator.adapters.pysnmp_agent import SnmpAgent, StoreInstrumentation
from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType


def test_reads_existing_metric() -> None:
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    instrumentation = StoreInstrumentation(store)

    result = instrumentation.read_variables((rfc1902.ObjectName(oid.parts), rfc1902.Null("")))

    assert int(result[0][1]) == 25
    assert result[0][1].tagSet == rfc1902.Gauge32.tagSet


def test_reads_next_metric() -> None:
    first = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    second = ObjectIdentifier.parse("1.3.6.1.4.1.55555.2.0")
    store = MetricStore(
        [
            Metric("first", first, SnmpDataType.INTEGER, 1),
            Metric("second", second, SnmpDataType.INTEGER, 2),
        ]
    )
    instrumentation = StoreInstrumentation(store)

    result = instrumentation.read_next_variables(
        (rfc1902.ObjectName(first.parts), rfc1902.Null(""))
    )

    assert tuple(result[0][0]) == second.parts
    assert int(result[0][1]) == 2


def reserve_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_answers_get_over_udp() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    next_oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.2.0")
    store = MetricStore(
        [
            Metric("cpu", oid, SnmpDataType.GAUGE32, 31),
            Metric("memory", next_oid, SnmpDataType.GAUGE32, 55),
        ]
    )
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    await agent.start()
    await asyncio.sleep(0.05)
    client_engine = engine.SnmpEngine()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
        error, status, _, var_binds = await get_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )
        assert error is None
        assert not status
        assert int(var_binds[0][1]) == 31

        error, status, _, var_binds = await next_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )
        assert error is None
        assert not status
        assert tuple(var_binds[0][0]) == next_oid.parts
        assert int(var_binds[0][1]) == 55

        error, status, _, var_binds = await bulk_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            0,
            1,
            ObjectType(ObjectIdentity(str(oid))),
        )
        assert error is None
        assert not status
        assert tuple(var_binds[0][0]) == next_oid.parts
        assert int(var_binds[0][1]) == 55
    finally:
        client_engine.close_dispatcher()
        await agent.stop()
