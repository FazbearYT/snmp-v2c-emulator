from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from pyasn1.codec.ber import encoder
from pyasn1.type import namedtype, univ
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto import rfc1901, rfc1902, rfc1905
from pysnmp.proto.api import v2c
from pysnmp.proto.mpmod.rfc2576 import SnmpV2cMessageProcessingModel
from pysnmp.smi import exval
from pysnmp.smi.instrum import AbstractMibInstrumController

from ..domain.metric import Metric
from ..domain.oid import ObjectIdentifier
from ..domain.store import MetricStore
from ..domain.types import SnmpDataType, encode_octet_string

LOGGER = logging.getLogger(__name__)


class LenientBulkPdu(rfc1905.BulkPDU):
    componentType = namedtype.NamedTypes(  # noqa: N815
        namedtype.NamedType("request-id", rfc1902.Integer32()),
        namedtype.NamedType("non-repeaters", univ.Integer()),
        namedtype.NamedType("max-repetitions", univ.Integer()),
        namedtype.NamedType("variable-bindings", rfc1905.VarBindList()),
    )


class LenientGetBulkRequestPdu(LenientBulkPdu):
    tagSet = rfc1905.GetBulkRequestPDU.tagSet  # noqa: N815


class V2cPdus(univ.Choice):
    componentType = namedtype.NamedTypes(  # noqa: N815
        namedtype.NamedType("get-request", rfc1905.GetRequestPDU()),
        namedtype.NamedType("get-next-request", rfc1905.GetNextRequestPDU()),
        namedtype.NamedType("get-bulk-request", LenientGetBulkRequestPdu()),
        namedtype.NamedType("response", rfc1905.ResponsePDU()),
        namedtype.NamedType("set-request", rfc1905.SetRequestPDU()),
        namedtype.NamedType("inform-request", rfc1905.InformRequestPDU()),
        namedtype.NamedType("snmpV2-trap", rfc1905.SNMPv2TrapPDU()),
        namedtype.NamedType("report", rfc1905.ReportPDU()),
    )


class V2cMessage(univ.Sequence):
    componentType = namedtype.NamedTypes(  # noqa: N815
        namedtype.NamedType("version", rfc1901.version),
        namedtype.NamedType("community", univ.OctetString()),
        namedtype.NamedType("data", V2cPdus()),
    )


class V2cMessageProcessingModel(SnmpV2cMessageProcessingModel):
    SNMP_MSG_SPEC = V2cMessage


class ResponseSizeGuard:
    def send_varbinds(
        self,
        snmp_engine: engine.SnmpEngine,
        state_reference: Any,
        error_status: Any,
        error_index: Any,
        var_binds: list[tuple[Any, Any]],
    ) -> None:
        pending_requests = self._CommandResponderBase__pendingReqs  # type: ignore[attr-defined]
        pending = pending_requests[state_reference]
        response_pdu = pending[7]
        max_response_size = int(pending[9])
        v2c.apiPDU.set_error_status(response_pdu, error_status)
        v2c.apiPDU.set_error_index(response_pdu, error_index)
        v2c.apiPDU.set_varbinds(response_pdu, var_binds)
        if len(encoder.encode(response_pdu)) > max_response_size:
            error_status = "tooBig"
            error_index = 0
            var_binds = []
        super().send_varbinds(  # type: ignore[misc]
            snmp_engine,
            state_reference,
            error_status,
            error_index,
            var_binds,
        )


class V2cGetCommandResponder(ResponseSizeGuard, cmdrsp.GetCommandResponder):
    pass


class V2cNextCommandResponder(ResponseSizeGuard, cmdrsp.NextCommandResponder):
    pass


class V2cBulkCommandResponder(ResponseSizeGuard, cmdrsp.BulkCommandResponder):
    def handle_management_operation(
        self,
        snmp_engine: engine.SnmpEngine,
        state_reference: Any,
        context_name: Any,
        pdu: Any,
    ) -> None:
        non_repeaters = max(int(v2c.apiBulkPDU.get_non_repeaters(pdu)), 0)
        max_repetitions = max(int(v2c.apiBulkPDU.get_max_repetitions(pdu)), 0)
        request_var_binds = v2c.apiPDU.get_varbinds(pdu)
        non_repeater_count = min(non_repeaters, len(request_var_binds))
        repeater_count = max(len(request_var_binds) - non_repeater_count, 0)
        if non_repeater_count == 0 and (max_repetitions == 0 or repeater_count == 0):
            self.send_varbinds(snmp_engine, state_reference, 0, 0, [])
            self.release_state_information(state_reference)
            return
        super().handle_management_operation(
            snmp_engine,
            state_reference,
            context_name,
            pdu,
        )


class ManagedUdpTransport(udp.UdpAsyncioTransport):
    def __init__(self) -> None:
        super().__init__()
        self._closed = asyncio.Event()

    async def wait_ready(self) -> None:
        if self._lport is None:
            raise RuntimeError("UDP transport has not been opened")
        await self._lport

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        self._closed.set()

    def datagram_received(self, datagram: bytes, transport_address: Any) -> None:
        if self._callback_function is None:
            super().datagram_received(datagram, transport_address)
            return
        self.loop.call_soon(self._process_datagram, transport_address, datagram)

    def _process_datagram(self, transport_address: Any, datagram: bytes) -> None:
        try:
            self._callback_function(self, transport_address, datagram)
        except Exception:
            LOGGER.debug(
                "discarded malformed SNMP datagram from %s",
                transport_address,
                exc_info=True,
            )


def bind_udp_socket(host: str, port: int) -> socket.socket:
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        server_socket.bind((host, port))
    except OSError as exc:
        server_socket.close()
        raise OSError(f"cannot bind udp://{host}:{port}: {exc}") from exc
    return server_socket


def encode_value(metric: Metric) -> Any:
    constructors = {
        SnmpDataType.INTEGER: rfc1902.Integer32,
        SnmpDataType.OCTET_STRING: rfc1902.OctetString,
        SnmpDataType.OBJECT_IDENTIFIER: rfc1902.ObjectIdentifier,
        SnmpDataType.IP_ADDRESS: rfc1902.IpAddress,
        SnmpDataType.COUNTER32: rfc1902.Counter32,
        SnmpDataType.GAUGE32: rfc1902.Gauge32,
        SnmpDataType.TIME_TICKS: rfc1902.TimeTicks,
        SnmpDataType.COUNTER64: rfc1902.Counter64,
    }
    value = metric.value
    if metric.data_type is SnmpDataType.OBJECT_IDENTIFIER:
        value = ObjectIdentifier.parse(value).parts
    elif metric.data_type is SnmpDataType.OCTET_STRING:
        value = encode_octet_string(value)
    return constructors[metric.data_type](value)


class StoreInstrumentation(AbstractMibInstrumController):
    def __init__(self, store: MetricStore) -> None:
        self.store = store

    def read_variables(
        self, *var_binds: tuple[Any, Any], **context_data: Any
    ) -> list[tuple[Any, Any]]:
        result: list[tuple[Any, Any]] = []
        for name, _ in var_binds:
            oid = ObjectIdentifier(tuple(int(part) for part in name))
            metric = self.store.get(oid)
            result.append((name, encode_value(metric) if metric else exval.noSuchObject))
        return result

    def read_next_variables(
        self,
        *var_binds: tuple[Any, Any],
        **context_data: Any,
    ) -> list[tuple[Any, Any]]:
        result: list[tuple[Any, Any]] = []
        for name, _ in var_binds:
            oid = ObjectIdentifier(tuple(int(part) for part in name))
            metric = self.store.get_next(oid)
            if metric is None:
                result.append((name, exval.endOfMibView))
            else:
                result.append((rfc1902.ObjectName(metric.oid.parts), encode_value(metric)))
        return result


class SnmpAgent:
    def __init__(self, host: str, port: int, community: str, store: MetricStore) -> None:
        self.host = host
        self.port = port
        self.community = community
        self.store = store
        self._lifecycle_lock = asyncio.Lock()
        self._engine: engine.SnmpEngine | None = None
        self._transport: ManagedUdpTransport | None = None

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._engine is not None:
                raise RuntimeError("SNMP agent is already running")
            server_socket = bind_udp_socket(self.host, self.port)
            snmp_engine = engine.SnmpEngine()
            try:
                snmp_engine.message_processing_subsystems.pop(0, None)
                snmp_engine.security_models.pop(1, None)
                snmp_engine.message_processing_subsystems[1] = V2cMessageProcessingModel()
                transport = ManagedUdpTransport()
                transport.open_server_mode(sock=server_socket)
                config.add_transport(
                    snmp_engine,
                    udp.DOMAIN_NAME,
                    transport,
                )
                config.add_v1_system(
                    snmp_engine,
                    "emulator",
                    encode_octet_string(self.community),
                )
                for oid_root in ((0,), (1,), (2,)):
                    config.add_vacm_user(
                        snmp_engine,
                        2,
                        "emulator",
                        "noAuthNoPriv",
                        readSubTree=oid_root,
                    )
                snmp_context = context.SnmpContext(snmp_engine)
                snmp_context.unregister_context_name(b"")
                snmp_context.register_context_name(b"", StoreInstrumentation(self.store))
                V2cGetCommandResponder(snmp_engine, snmp_context)
                V2cNextCommandResponder(snmp_engine, snmp_context)
                V2cBulkCommandResponder(snmp_engine, snmp_context)
                await transport.wait_ready()
            except BaseException:
                snmp_engine.close_dispatcher()
                server_socket.close()
                raise
            self._engine = snmp_engine
            self._transport = transport
            LOGGER.info("listening on udp://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._engine is not None:
                transport = self._transport
                self._engine.close_dispatcher()
                self._engine = None
                self._transport = None
                if transport is not None:
                    await transport.wait_closed()
