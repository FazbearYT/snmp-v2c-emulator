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
from pysnmp.smi import error as smi_error
from pysnmp.smi import exval
from pysnmp.smi.instrum import AbstractMibInstrumController

from ..domain.metric import Metric
from ..domain.oid import ObjectIdentifier
from ..domain.store import MetricStore
from ..domain.types import SnmpDataType, encode_octet_string
from ..run_report import RequestCounters
from .request_audit import SIMULATED_GENERR_RESPONSE, RequestAudit

LOGGER = logging.getLogger(__name__)
MAX_UDP_PAYLOAD_BYTES = 65507


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
    truncate_varbinds = False

    def __init__(self, *args: Any, community: bytes, **kwargs: Any) -> None:
        self._community = community
        super().__init__(*args, **kwargs)

    def _response_fits(
        self, response_pdu: Any, var_binds: list[tuple[Any, Any]], max_response_size: int
    ) -> bool:
        v2c.apiPDU.set_varbinds(response_pdu, var_binds)
        maximum_request_id_pdu = response_pdu.clone(cloneValueFlag=True)
        v2c.apiPDU.set_request_id(maximum_request_id_pdu, 2**31 - 1)
        v2c.apiPDU.set_varbinds(maximum_request_id_pdu, var_binds)
        response_message = v2c.Message()
        v2c.apiMessage.set_defaults(response_message)
        v2c.apiMessage.set_community(response_message, self._community)
        v2c.apiMessage.set_pdu(response_message, maximum_request_id_pdu)
        return (
            len(encoder.encode(response_pdu)) <= max_response_size
            and len(encoder.encode(response_message)) <= MAX_UDP_PAYLOAD_BYTES
        )

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
        if not self._response_fits(response_pdu, var_binds, max_response_size):
            if self.truncate_varbinds and not error_status:
                lower, upper = 0, len(var_binds)
                while lower < upper:
                    middle = (lower + upper + 1) // 2
                    if self._response_fits(response_pdu, var_binds[:middle], max_response_size):
                        lower = middle
                    else:
                        upper = middle - 1
                var_binds = var_binds[:lower]
            else:
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


class SimulatedOidGenError(smi_error.GenError):
    """A genErr explicitly triggered by an OID response mode."""


class SimulatedErrorResponder:
    """Translate instrumentation failures into SNMP genErr responses."""

    def _send_gen_error(
        self,
        snmp_engine: engine.SnmpEngine,
        state_reference: Any,
        pdu: Any,
        error: Any,
        original: list[tuple[Any, Any]] | None = None,
    ) -> None:
        if original is None:
            original = v2c.apiPDU.get_varbinds(pdu)
        index = int(error["idx"]) + 1
        if not 1 <= index <= len(original):
            index = 1
        marker = SIMULATED_GENERR_RESPONSE.set(isinstance(error, SimulatedOidGenError))
        try:
            self.send_varbinds(snmp_engine, state_reference, "genErr", index, original)
        finally:
            SIMULATED_GENERR_RESPONSE.reset(marker)
        self.release_state_information(state_reference)

    def handle_management_operation(
        self,
        snmp_engine: engine.SnmpEngine,
        state_reference: Any,
        context_name: Any,
        pdu: Any,
    ) -> None:
        try:
            super().handle_management_operation(  # type: ignore[misc]
                snmp_engine, state_reference, context_name, pdu
            )
        except smi_error.GenError as exc:
            self._send_gen_error(snmp_engine, state_reference, pdu, exc)


class V2cGetCommandResponder(
    SimulatedErrorResponder, ResponseSizeGuard, cmdrsp.GetCommandResponder
):
    pass


class V2cNextCommandResponder(
    SimulatedErrorResponder, ResponseSizeGuard, cmdrsp.NextCommandResponder
):
    pass


class V2cBulkCommandResponder(
    SimulatedErrorResponder, ResponseSizeGuard, cmdrsp.BulkCommandResponder
):
    truncate_varbinds = True

    def handle_management_operation(
        self,
        snmp_engine: engine.SnmpEngine,
        state_reference: Any,
        context_name: Any,
        pdu: Any,
    ) -> None:
        non_repeaters = max(int(v2c.apiBulkPDU.get_non_repeaters(pdu)), 0)
        repetitions = max(int(v2c.apiBulkPDU.get_max_repetitions(pdu)), 0)
        original = v2c.apiPDU.get_varbinds(pdu)
        requested = original[: self.max_varbinds]
        fixed = min(non_repeaters, len(requested))
        repeating = len(requested) - fixed
        if repeating:
            repetitions = min(repetitions, (self.max_varbinds - fixed) // repeating)
        v2c.apiBulkPDU.set_non_repeaters(pdu, fixed)
        v2c.apiBulkPDU.set_max_repetitions(pdu, repetitions)
        v2c.apiPDU.set_varbinds(pdu, requested)

        instrumentation = self.snmpContext.get_mib_instrum(context_name)
        context_data = {
            "snmpEngine": snmp_engine,
            "acFun": self.verify_access,
            "cbCtx": self.cbCtx,
        }
        try:
            result = (
                instrumentation.read_next_variables(*requested[:fixed], **context_data)
                if fixed
                else []
            )
            cursors = requested[fixed:]
            for _ in range(repetitions if repeating else 0):
                row = instrumentation.read_next_variables(
                    *cursors, index_offset=fixed, **context_data
                )
                result.extend(row)
                cursors = row
            # A valid zero-repetition GETBULK has an empty response.
            self.send_varbinds(snmp_engine, state_reference, 0, 0, result)
            self.release_state_information(state_reference)
        except smi_error.GenError as exc:
            # Echo the original request, even when GETBULK processing was clamped.
            self._send_gen_error(snmp_engine, state_reference, pdu, exc, original)


class ManagedUdpTransport(udp.UdpAsyncioTransport):
    def __init__(self) -> None:
        super().__init__()
        self._closed = asyncio.Event()
        self._request_audit: RequestAudit | None = None
        self._unavailable = False

    async def wait_ready(self) -> None:
        if self._lport is None:
            raise RuntimeError("UDP transport has not been opened")
        await self._lport

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        self._closed.set()

    def send_message(self, outgoing_message: bytes, transport_address: Any) -> Any:
        if self._unavailable:
            if self._request_audit is not None:
                self._request_audit.response(
                    outgoing_message, transport_address, sent=False, suppressed=True
                )
            return None
        try:
            result = super().send_message(outgoing_message, transport_address)
        except Exception:
            if self._request_audit is not None:
                self._request_audit.response(outgoing_message, transport_address, sent=False)
            raise
        if self._request_audit is not None:
            self._request_audit.response(outgoing_message, transport_address, sent=True)
        return result

    def datagram_received(self, datagram: bytes, transport_address: Any) -> None:
        if self._request_audit is not None:
            self._request_audit.received(datagram, transport_address)
        if self._unavailable:
            if self._request_audit is not None:
                self._request_audit.suppress_datagram(datagram, transport_address)
            return
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
    def __init__(self, store: MetricStore, oid_responses: dict[str, str] | None = None) -> None:
        self.store = store
        self.oid_responses = {
            ObjectIdentifier.parse(oid): mode for oid, mode in (oid_responses or {}).items()
        }

    def _mode(self, oid: ObjectIdentifier) -> str:
        return self.oid_responses.get(oid, "normal")

    def _missing_value(self, oid: ObjectIdentifier) -> Any:
        # A scalar with the same object identifier but a different instance
        # is an absent instance; a completely unknown object is absent.
        for metric in self.store.snapshot():
            if metric.oid.parts[:-1] == oid.parts[:-1]:
                return exval.noSuchInstance
        return exval.noSuchObject

    def read_variables(
        self, *var_binds: tuple[Any, Any], **context_data: Any
    ) -> list[tuple[Any, Any]]:
        result: list[tuple[Any, Any]] = []
        for index, (name, _) in enumerate(var_binds):
            oid = ObjectIdentifier(tuple(int(part) for part in name))
            metric = self.store.get(oid)
            if metric is None:
                value = self._missing_value(oid)
            else:
                mode = self._mode(oid)
                if mode == "genErr":
                    raise SimulatedOidGenError(idx=index)
                if mode == "noSuchObject":
                    value = exval.noSuchObject
                elif mode == "noSuchInstance":
                    value = exval.noSuchInstance
                else:
                    value = encode_value(metric)
            result.append((name, value))
        return result

    def read_next_variables(
        self, *var_binds: tuple[Any, Any], **context_data: Any
    ) -> list[tuple[Any, Any]]:
        result: list[tuple[Any, Any]] = []
        index_offset = int(context_data.get("index_offset", 0))
        for index, (name, _) in enumerate(var_binds):
            cursor = ObjectIdentifier(tuple(int(part) for part in name))
            while True:
                metric = self.store.get_next(cursor)
                if metric is None:
                    result.append((name, exval.endOfMibView))
                    break
                mode = self._mode(metric.oid)
                if mode == "genErr":
                    raise SimulatedOidGenError(idx=index + index_offset)
                if mode in {"noSuchObject", "noSuchInstance"}:
                    cursor = metric.oid
                    continue
                result.append((rfc1902.ObjectName(metric.oid.parts), encode_value(metric)))
                break
        return result


class SnmpAgent:
    def __init__(
        self,
        host: str,
        port: int,
        community: str,
        store: MetricStore,
        oid_responses: dict[str, str] | None = None,
        request_counters: RequestCounters | None = None,
    ) -> None:
        self.request_counters = request_counters
        self.host = host
        self.port = port
        self.community = community
        self.store = store
        self.oid_responses = oid_responses or {}
        self._lifecycle_lock = asyncio.Lock()
        self._engine: engine.SnmpEngine | None = None
        self._transport: ManagedUdpTransport | None = None
        self._unavailable = False

    @property
    def unavailable(self) -> bool:
        return self._unavailable

    def set_unavailable(self, unavailable: bool) -> None:
        """Do not close the UDP socket: clients must observe real timeouts."""
        if self._unavailable == unavailable:
            return
        self._unavailable = unavailable
        if self._transport is not None:
            self._transport._unavailable = unavailable
        LOGGER.info("SNMP availability changed: %s", "unavailable" if unavailable else "available")

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._engine is not None:
                raise RuntimeError("SNMP agent is already running")
            server_socket = bind_udp_socket(self.host, self.port)
            snmp_engine = None
            try:
                snmp_engine = engine.SnmpEngine()
                snmp_engine.message_processing_subsystems.pop(0, None)
                snmp_engine.security_models.pop(1, None)
                snmp_engine.message_processing_subsystems[1] = V2cMessageProcessingModel()
                transport = ManagedUdpTransport()
                transport._request_audit = RequestAudit(  # shared by CLI and GUI
                    encode_octet_string(self.community),
                    V2cMessage,
                    counters=self.request_counters,
                )
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
                snmp_context.register_context_name(
                    b"", StoreInstrumentation(self.store, self.oid_responses)
                )
                community = encode_octet_string(self.community)
                V2cGetCommandResponder(snmp_engine, snmp_context, community=community)
                V2cNextCommandResponder(snmp_engine, snmp_context, community=community)
                V2cBulkCommandResponder(snmp_engine, snmp_context, community=community)
                await transport.wait_ready()
            except BaseException:
                if snmp_engine is not None:
                    snmp_engine.close_dispatcher()
                server_socket.close()
                raise
            self._engine = snmp_engine
            self._transport = transport
            self._unavailable = False

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._engine is not None:
                transport = self._transport
                self._engine.close_dispatcher()
                self._engine = None
                self._transport = None
                self._unavailable = False
                if transport is not None:
                    await transport.wait_closed()
                    if transport._request_audit is not None:
                        transport._request_audit.close()
