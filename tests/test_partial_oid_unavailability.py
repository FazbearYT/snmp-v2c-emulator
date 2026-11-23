"""SNMPv2c partial-OID availability: config, GET, GETNEXT and GETBULK."""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest
from pyasn1.codec.ber import decoder, encoder
from pydantic import ValidationError
from pysnmp.proto import rfc1902
from pysnmp.proto.api import v2c
from pysnmp.smi import error as smi_error
from pysnmp.smi import exval

from snmp_emulator.adapters.pysnmp_agent import StoreInstrumentation, V2cMessage
from snmp_emulator.application import EmulatorApplication
from snmp_emulator.config import EmulatorConfig
from snmp_emulator.json_config import DeviceConfig, ScenarioDocument, bind_config

ROOT = "1.3.6.1.4.1.55555"
A, B, C, D = (f"{ROOT}.{n}.0" for n in range(1, 5))


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_config(modes: dict[str, str]) -> EmulatorConfig:
    device = DeviceConfig.model_validate(
        {
            "agent": {"host": "127.0.0.1", "port": reserve_port()},
            "metrics": [
                {"name": str(n), "oid": oid, "type": "Integer32", "initial": n}
                for n, oid in enumerate((A, B, C, D), 1)
            ],
        }
    )
    scenario = ScenarioDocument.model_validate(
        {
            "duration": 2.0,
            "update_interval": 0.1,
            "rules": [] if modes else [{"type": "constant", "oid": A, "value": 1}],
            "oid_responses": [{"oid": oid, "mode": mode} for oid, mode in modes.items()],
        }
    )
    return bind_config(device, scenario)


def request(pdu_type, oids: list[str], *, non_repeaters=0, repetitions=2) -> bytes:
    pdu = pdu_type()
    if pdu_type is v2c.GetBulkRequestPDU:
        v2c.apiBulkPDU.set_defaults(pdu)
        v2c.apiBulkPDU.set_non_repeaters(pdu, non_repeaters)
        v2c.apiBulkPDU.set_max_repetitions(pdu, repetitions)
    else:
        v2c.apiPDU.set_defaults(pdu)
    v2c.apiPDU.set_request_id(pdu, 109)
    v2c.apiPDU.set_varbinds(pdu, [(rfc1902.ObjectName(oid), rfc1902.Null("")) for oid in oids])
    message = V2cMessage()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, b"public")
    v2c.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


async def exchange(sock, port: int, payload: bytes):
    loop = asyncio.get_running_loop()
    await loop.sock_sendto(sock, payload, ("127.0.0.1", port))
    data = await asyncio.wait_for(loop.sock_recv(sock, 65535), timeout=1)
    message, trailing = decoder.decode(data, asn1Spec=V2cMessage())
    assert not trailing
    return v2c.apiMessage.get_pdu(message)


def values(pdu) -> list[tuple[str, object]]:
    return [(str(oid), value) for oid, value in v2c.apiPDU.get_varbinds(pdu)]


@pytest.mark.asyncio
async def test_mixed_get_keeps_values_and_two_distinct_exceptions(caplog):
    config = make_config({B: "noSuchObject", C: "noSuchInstance"})
    app = EmulatorApplication(config)
    caplog.set_level(logging.INFO, logger="snmp_emulator.adapters.request_audit")
    await app.agent.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            pdu = await exchange(
                sock,
                config.agent.port,
                request(v2c.GetRequestPDU, [A, B, C, D]),
            )
        assert int(v2c.apiPDU.get_error_status(pdu)) == 0
        response = values(pdu)
        assert [oid for oid, _ in response] == [A, B, C, D]
        assert [int(response[i][1]) for i in (0, 3)] == [1, 4]
        assert response[1][1].tagSet == exval.noSuchObject.tagSet
        assert response[2][1].tagSet == exval.noSuchInstance.tagSet
        logs = [entry.getMessage() for entry in caplog.records]
        assert any("noSuchObject" in line and "noSuchInstance" in line for line in logs)
    finally:
        await app.agent.stop()


@pytest.mark.asyncio
async def test_get_generr_uses_original_request_index_and_audit(caplog):
    config = make_config({C: "genErr"})
    app = EmulatorApplication(config)
    caplog.set_level(logging.INFO, logger="snmp_emulator.adapters.request_audit")
    await app.agent.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            pdu = await exchange(
                sock,
                config.agent.port,
                request(v2c.GetRequestPDU, [A, C, D]),
            )
        assert int(v2c.apiPDU.get_error_status(pdu)) == 5
        assert int(v2c.apiPDU.get_error_index(pdu)) == 2
        assert [oid for oid, _ in values(pdu)] == [A, C, D]
        assert any(
            "error_status=5 error_index=2" in record.getMessage() for record in caplog.records
        )
    finally:
        await app.agent.stop()


@pytest.mark.asyncio
async def test_getnext_skips_absent_and_bulk_preserves_lexical_order():
    config = make_config({B: "noSuchObject", C: "noSuchInstance"})
    agent = EmulatorApplication(config).agent
    await agent.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            nxt = await exchange(
                sock,
                config.agent.port,
                request(v2c.GetNextRequestPDU, [A]),
            )
            assert values(nxt)[0][0] == D
            assert int(values(nxt)[0][1]) == 4
            bulk = await exchange(
                sock,
                config.agent.port,
                request(v2c.GetBulkRequestPDU, [ROOT], repetitions=3),
            )
            assert [oid for oid, _ in values(bulk)[:2]] == [A, D]
            assert values(bulk)[-1][1].tagSet == exval.endOfMibView.tagSet
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_next_and_bulk_generr_use_correct_variable_index():
    config = make_config({C: "genErr"})
    agent = EmulatorApplication(config).agent
    await agent.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            nxt = await exchange(
                sock,
                config.agent.port,
                request(v2c.GetNextRequestPDU, [D, B]),
            )
            assert int(v2c.apiPDU.get_error_status(nxt)) == 5
            assert int(v2c.apiPDU.get_error_index(nxt)) == 2
            bulk = await exchange(
                sock,
                config.agent.port,
                request(
                    v2c.GetBulkRequestPDU,
                    [D, B],
                    non_repeaters=1,
                    repetitions=1,
                ),
            )
            assert int(v2c.apiPDU.get_error_status(bulk)) == 5
            assert int(v2c.apiPDU.get_error_index(bulk)) == 2
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_failed_oid_mode_does_not_persist_into_new_agent():
    config = make_config({B: "genErr"})
    first = EmulatorApplication(config)
    second = EmulatorApplication(make_config({}))
    assert first.agent.oid_responses[B] == "genErr"
    assert second.agent.oid_responses == {}
    await first.agent.start()
    await first.agent.stop()
    await second.agent.start()
    await second.agent.stop()


@pytest.mark.parametrize(
    "responses",
    [
        [{"oid": B, "mode": "bogus"}],
        [{"oid": B, "mode": "genErr"}, {"oid": B, "mode": "normal"}],
        [{"oid": "not-an-oid", "mode": "genErr"}],
    ],
)
def test_invalid_oid_responses_are_rejected(responses):
    with pytest.raises(ValidationError):
        ScenarioDocument.model_validate(
            {
                "duration": 1.0,
                "update_interval": 0.1,
                "rules": [],
                "oid_responses": responses,
            }
        )


def test_unknown_oid_mode_target_is_rejected_during_binding():
    device = DeviceConfig.model_validate(
        {
            "agent": {},
            "metrics": [{"name": "cpu", "oid": A, "type": "Integer32", "initial": 1}],
        }
    )
    scenario = ScenarioDocument.model_validate(
        {
            "duration": 1.0,
            "update_interval": 0.1,
            "oid_responses": [{"oid": B, "mode": "genErr"}],
        }
    )
    with pytest.raises(ValidationError, match="unknown OID response target"):
        bind_config(device, scenario)


def test_missing_instance_of_known_scalar():
    config = make_config({})
    instrumentation = StoreInstrumentation(EmulatorApplication(config).store)
    missing = rfc1902.ObjectName(f"{ROOT}.1.1")
    response = instrumentation.read_variables((missing, rfc1902.Null("")))
    assert response[0][1].tagSet == exval.noSuchInstance.tagSet


def test_generr_instrumentation_raises_with_correct_index():
    config = make_config({C: "genErr"})
    instrument = StoreInstrumentation(EmulatorApplication(config).store, {C: "genErr"})
    with pytest.raises(smi_error.GenError) as failure:
        instrument.read_variables(
            (rfc1902.ObjectName(A), rfc1902.Null("")),
            (rfc1902.ObjectName(C), rfc1902.Null("")),
        )
    assert int(failure.value["idx"]) == 1



@pytest.mark.asyncio
async def test_getbulk_error_echoes_original_varbinds_after_budget_clamp():
    """A failing GETBULK response retains every original request binding."""
    config = make_config({C: "genErr"})
    agent = EmulatorApplication(config).agent
    await agent.start()
    try:
        oids = [A, B] + [D] * 63
        pdu = v2c.GetBulkRequestPDU()
        v2c.apiBulkPDU.set_defaults(pdu)
        v2c.apiPDU.set_request_id(pdu, 109)
        v2c.apiBulkPDU.set_non_repeaters(pdu, 0)
        v2c.apiBulkPDU.set_max_repetitions(pdu, 1)
        v2c.apiPDU.set_varbinds(
            pdu, [(rfc1902.ObjectName(oid), rfc1902.Null("")) for oid in oids]
        )
        message = V2cMessage()
        v2c.apiMessage.set_defaults(message)
        v2c.apiMessage.set_community(message, b"public")
        v2c.apiMessage.set_pdu(message, pdu)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            # The explicit non-repeater test exercises error-status with a
            # batch of more than 64 request bindings.
            pdu = await exchange(sock, config.agent.port, encoder.encode(message))
        assert int(v2c.apiPDU.get_error_status(pdu)) == 5
        assert int(v2c.apiPDU.get_error_index(pdu)) == 2
        assert [str(oid) for oid, _ in v2c.apiPDU.get_varbinds(pdu)] == oids
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_mixed_get_with_generr_preempts_per_binding_exceptions():
    config = make_config({B: "noSuchObject", C: "genErr"})
    agent = EmulatorApplication(config).agent
    await agent.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            pdu = await exchange(
                sock, config.agent.port, request(v2c.GetRequestPDU, [B, C, D])
            )
        assert int(v2c.apiPDU.get_error_status(pdu)) == 5
        assert int(v2c.apiPDU.get_error_index(pdu)) == 2
        assert [str(oid) for oid, _ in v2c.apiPDU.get_varbinds(pdu)] == [B, C, D]
    finally:
        await agent.stop()
