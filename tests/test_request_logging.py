import asyncio
import logging
import socket

import pytest
from pyasn1.codec.ber import decoder, encoder
from pysnmp.proto import rfc1902
from pysnmp.proto.api import v2c

from snmp_emulator.adapters.pysnmp_agent import SnmpAgent, V2cMessage
from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType

OID = "1.3.6.1.4.1.55555.1.0"
MISSING = "1.3.6.1.4.1.55555.99.0"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(pdu_type, oid=OID, *, community=b"public", request_id=71):
    pdu = pdu_type()
    if pdu_type is v2c.GetBulkRequestPDU:
        v2c.apiBulkPDU.set_defaults(pdu)
    else:
        v2c.apiPDU.set_defaults(pdu)
    v2c.apiPDU.set_request_id(pdu, request_id)
    if pdu_type is v2c.GetBulkRequestPDU:
        v2c.apiBulkPDU.set_non_repeaters(pdu, 0)
        v2c.apiBulkPDU.set_max_repetitions(pdu, 2)
    v2c.apiPDU.set_varbinds(pdu, [(rfc1902.ObjectName(oid), rfc1902.Null(""))])
    message = V2cMessage()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, community)
    v2c.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


async def exchange(sock, port, payload):
    loop = asyncio.get_running_loop()
    await loop.sock_sendto(sock, payload, ("127.0.0.1", port))
    response = await asyncio.wait_for(loop.sock_recv(sock, 65535), 1)
    message, trailing = decoder.decode(response, asn1Spec=V2cMessage())
    assert not trailing
    return v2c.apiMessage.get_pdu(message)


@pytest.mark.asyncio
async def test_udp_audit_records_get_getnext_bulk_and_exceptions(caplog):
    port = free_port()
    metric = Metric("cpu", ObjectIdentifier.parse(OID), SnmpDataType.INTEGER32, 42)
    agent = SnmpAgent("127.0.0.1", port, "public", MetricStore([metric]))
    caplog.set_level(logging.INFO, logger="snmp_emulator.adapters.request_audit")
    await agent.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            for pdu_type, oid in (
                (v2c.GetRequestPDU, OID),
                (v2c.GetRequestPDU, MISSING),
                (v2c.GetNextRequestPDU, MISSING),
                (v2c.GetBulkRequestPDU, "1.3.6.1.4.1.55555"),
            ):
                await exchange(sock, port, request(pdu_type, oid))
        received = [r.message for r in caplog.records if "event=received" in r.message]
        results = [r.message for r in caplog.records if "event=result" in r.message]
        assert len(received) == len(results) == 4
        assert all("source=127.0.0.1:" in line and "request_id=71" in line for line in received)
        assert "pdu=GETNEXT" in " ".join(received)
        assert "pdu=GETBULK" in " ".join(received)
        assert all("outcome=sent" in line for line in results)
        assert any("noSuchObject" in line for line in results)
        assert any("endOfMibView" in line for line in results)
        assert all("error_status=0 error_index=0" in line for line in results)
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_audit_counts_repeated_id_separately_and_excludes_wrong_community(caplog):
    port = free_port()
    metric = Metric("cpu", ObjectIdentifier.parse(OID), SnmpDataType.INTEGER32, 42)
    agent = SnmpAgent("127.0.0.1", port, "secret", MetricStore([metric]))
    caplog.set_level(logging.INFO, logger="snmp_emulator.adapters.request_audit")
    await agent.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            payload = request(v2c.GetRequestPDU, community=b"secret")
            await exchange(sock, port, payload)
            await exchange(sock, port, payload)
            await asyncio.get_running_loop().sock_sendto(
                sock, request(v2c.GetRequestPDU, community=b"wrong"), ("127.0.0.1", port)
            )
            await asyncio.sleep(0.05)
    finally:
        await agent.stop()
    received = [r.message for r in caplog.records if "event=received" in r.message]
    results = [r.message for r in caplog.records if "event=result" in r.message]
    assert len(received) == len(results) == 2
    assert len({line.split("token=")[1].split()[0] for line in received}) == 2
    assert all("outcome=sent" in line for line in results)
    assert not any("secret" in line or "wrong" in line for line in received + results)


@pytest.mark.asyncio
async def test_audit_clears_pending_on_restart(caplog):
    port = free_port()
    metric = Metric("cpu", ObjectIdentifier.parse(OID), SnmpDataType.INTEGER32, 42)
    agent = SnmpAgent("127.0.0.1", port, "public", MetricStore([metric]))
    caplog.set_level(logging.INFO, logger="snmp_emulator.adapters.request_audit")
    for _ in range(2):
        await agent.start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            await exchange(sock, port, request(v2c.GetRequestPDU))
        await agent.stop()
    received = [r.message for r in caplog.records if "event=received" in r.message]
    results = [r.message for r in caplog.records if "event=result" in r.message]
    assert len(received) == len(results) == 2
    assert all("outcome=sent" in line for line in results)
