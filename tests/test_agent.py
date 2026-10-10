import asyncio
import socket
from typing import Any

import pytest
from pyasn1.codec.ber import decoder, encoder
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
from pysnmp.proto import rfc1901, rfc1902
from pysnmp.proto.api import v2c
from pysnmp.smi import exval

from snmp_emulator.adapters.pysnmp_agent import (
    LenientGetBulkRequestPdu,
    ManagedUdpTransport,
    SnmpAgent,
    StoreInstrumentation,
    V2cMessage,
)
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


def test_encodes_unicode_octet_string_as_utf8() -> None:
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("label", oid, SnmpDataType.OCTET_STRING, "привет")])
    instrumentation = StoreInstrumentation(store)

    result = instrumentation.read_variables((rfc1902.ObjectName(oid.parts), rfc1902.Null("")))

    assert result[0][1].asOctets() == "привет".encode()


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


def test_returns_protocol_exceptions_for_missing_metrics() -> None:
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    instrumentation = StoreInstrumentation(store)
    missing_oid = rfc1902.ObjectName("1.3.6.1.4.1.55555.99.0")

    exact_result = instrumentation.read_variables((missing_oid, rfc1902.Null("")))
    next_result = instrumentation.read_next_variables((missing_oid, rfc1902.Null("")))

    assert exact_result[0][1].tagSet == exval.noSuchObject.tagSet
    assert next_result[0][1].tagSet == exval.endOfMibView.tagSet


def reserve_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def encode_bulk_request(
    oid: ObjectIdentifier,
    non_repeaters: int,
    max_repetitions: int,
) -> bytes:
    pdu = LenientGetBulkRequestPdu()
    v2c.apiBulkPDU.set_defaults(pdu)
    v2c.apiBulkPDU.set_non_repeaters(pdu, non_repeaters)
    v2c.apiBulkPDU.set_max_repetitions(pdu, max_repetitions)
    v2c.apiPDU.set_varbinds(pdu, [(rfc1902.ObjectName(oid.parts), rfc1902.Null(""))])
    message = V2cMessage()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, b"public")
    v2c.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


async def exchange_raw_datagram(port: int, payload: bytes) -> Any:
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        await loop.sock_sendto(sock, payload, ("127.0.0.1", port))
        response = await asyncio.wait_for(loop.sock_recv(sock, 65535), timeout=1)
    message, trailing = decoder.decode(response, asn1Spec=rfc1901.Message())
    assert not trailing
    return v2c.apiMessage.get_pdu(message)


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


@pytest.mark.asyncio
async def test_rejects_snmp_v1_request() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 31)])
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    await agent.start()
    client_engine = engine.SnmpEngine()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=0.1, retries=0)
        error, status, _, var_binds = await get_cmd(
            client_engine,
            CommunityData("public", mpModel=0),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )

        assert error is not None
        assert not status
        assert not var_binds
    finally:
        client_engine.close_dispatcher()
        await agent.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("non_repeaters", "max_repetitions", "expected_values"),
    [(0, 0, []), (0, -1, []), (-1, 1, [2])],
)
async def test_normalizes_getbulk_boundaries(
    non_repeaters: int,
    max_repetitions: int,
    expected_values: list[int],
) -> None:
    port = reserve_udp_port()
    first = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    second = ObjectIdentifier.parse("1.3.6.1.4.1.55555.2.0")
    store = MetricStore(
        [
            Metric("first", first, SnmpDataType.INTEGER, 1),
            Metric("second", second, SnmpDataType.INTEGER, 2),
        ]
    )
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    await agent.start()
    try:
        response_pdu = await exchange_raw_datagram(
            port,
            encode_bulk_request(first, non_repeaters, max_repetitions),
        )

        assert int(v2c.apiPDU.get_error_status(response_pdu)) == 0
        assert int(v2c.apiPDU.get_error_index(response_pdu)) == 0
        assert [int(value) for _, value in v2c.apiPDU.get_varbinds(response_pdu)] == (
            expected_values
        )
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_returns_too_big_for_oversized_response() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("payload", oid, SnmpDataType.OCTET_STRING, "x" * 65535)])
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    await agent.start()
    client_engine = engine.SnmpEngine()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
        error, status, index, var_binds = await get_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )

        assert error is None
        assert status.prettyPrint() == "tooBig"
        assert int(index) == 0
        assert not var_binds
    finally:
        client_engine.close_dispatcher()
        await agent.stop()


@pytest.mark.asyncio
async def test_answers_with_unicode_community_and_octet_string() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("label", oid, SnmpDataType.OCTET_STRING, "привет")])
    community = "тест"
    agent = SnmpAgent("127.0.0.1", port, community, store)
    await agent.start()
    client_engine = engine.SnmpEngine()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
        error, status, _, var_binds = await get_cmd(
            client_engine,
            CommunityData(community.encode(), mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )

        assert error is None
        assert not status
        assert var_binds[0][1].asOctets() == "привет".encode()
    finally:
        client_engine.close_dispatcher()
        await agent.stop()


@pytest.mark.asyncio
async def test_answers_for_oid_outside_mib_2_subtree() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("2.999.1.0")
    store = MetricStore([Metric("alternate_root", oid, SnmpDataType.INTEGER, 7)])
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
        assert int(var_binds[0][1]) == 7
    finally:
        client_engine.close_dispatcher()
        await agent.stop()


@pytest.mark.asyncio
async def test_handles_multi_varbind_and_protocol_boundaries() -> None:
    port = reserve_udp_port()
    first = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    second = ObjectIdentifier.parse("1.3.6.1.4.1.55555.2.0")
    third = ObjectIdentifier.parse("1.3.6.1.4.1.55555.3.0")
    missing = ObjectIdentifier.parse("1.3.6.1.4.1.55555.99.0")
    store = MetricStore(
        [
            Metric("first", first, SnmpDataType.INTEGER, 1),
            Metric("second", second, SnmpDataType.INTEGER, 2),
            Metric("third", third, SnmpDataType.INTEGER, 3),
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
            ObjectType(ObjectIdentity(str(first))),
            ObjectType(ObjectIdentity(str(third))),
            ObjectType(ObjectIdentity(str(missing))),
        )
        assert error is None
        assert not status
        assert [int(var_binds[index][1]) for index in (0, 1)] == [1, 3]
        assert var_binds[2][1].tagSet == exval.noSuchObject.tagSet

        error, status, _, var_binds = await next_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(third))),
        )
        assert error is None
        assert not status
        assert var_binds[0][1].tagSet == exval.endOfMibView.tagSet

        error, status, _, var_binds = await bulk_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            0,
            3,
            ObjectType(ObjectIdentity("1.3.6.1.4.1.55555.0")),
        )
        assert error is None
        assert not status
        assert [tuple(item[0]) for item in var_binds] == [first.parts, second.parts, third.parts]
        assert [int(item[1]) for item in var_binds] == [1, 2, 3]
    finally:
        client_engine.close_dispatcher()
        await agent.stop()


@pytest.mark.asyncio
async def test_ignores_request_with_wrong_community() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    await agent.start()
    await asyncio.sleep(0.05)
    client_engine = engine.SnmpEngine()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=0.1, retries=0)
        error, status, _, var_binds = await get_cmd(
            client_engine,
            CommunityData("wrong", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )
        assert error is not None
        assert not status
        assert not var_binds
    finally:
        client_engine.close_dispatcher()
        await agent.stop()


@pytest.mark.asyncio
async def test_rejects_second_agent_on_same_udp_port() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    first = SnmpAgent("127.0.0.1", port, "public", store)
    second = SnmpAgent("127.0.0.1", port, "public", store)
    await first.start()
    try:
        with pytest.raises(OSError, match="cannot bind"):
            await second.start()
    finally:
        await first.stop()


@pytest.mark.asyncio
async def test_rejects_starting_same_agent_twice() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    await agent.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await agent.start()
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_stop_releases_udp_port_before_returning() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    first = SnmpAgent("127.0.0.1", port, "public", store)
    second = SnmpAgent("127.0.0.1", port, "public", store)

    await first.start()
    await first.stop()
    await second.start()
    await second.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_start_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    start_paused = asyncio.Event()
    resume_start = asyncio.Event()
    original_wait_ready = ManagedUdpTransport.wait_ready

    async def pause_after_transport_is_ready(transport: ManagedUdpTransport) -> None:
        await original_wait_ready(transport)
        start_paused.set()
        await resume_start.wait()

    monkeypatch.setattr(ManagedUdpTransport, "wait_ready", pause_after_transport_is_ready)
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    start_task = asyncio.create_task(agent.start())
    await start_paused.wait()
    stop_task = asyncio.create_task(agent.stop())
    await asyncio.sleep(0)

    assert not stop_task.done()

    resume_start.set()
    await start_task
    await stop_task

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            replacement.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        replacement.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_discards_malformed_datagram_without_loop_error() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    await agent.start()
    client_engine = engine.SnmpEngine()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"\x30\x82\xff\xff", ("127.0.0.1", port))
        await asyncio.sleep(0.05)

        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
        error, status, _, var_binds = await get_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )

        assert not loop_errors
        assert error is None
        assert not status
        assert int(var_binds[0][1]) == 25
    finally:
        loop.set_exception_handler(previous_handler)
        client_engine.close_dispatcher()
        await agent.stop()


@pytest.mark.asyncio
async def test_cancellation_during_start_releases_udp_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    wait_entered = asyncio.Event()

    async def wait_forever(_transport: ManagedUdpTransport) -> None:
        wait_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ManagedUdpTransport, "wait_ready", wait_forever)
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    start_task = asyncio.create_task(agent.start())
    await wait_entered.wait()
    start_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await start_task

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            replacement.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        replacement.bind(("127.0.0.1", port))
