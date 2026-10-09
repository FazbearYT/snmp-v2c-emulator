from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto import rfc1902
from pysnmp.smi import exval
from pysnmp.smi.instrum import AbstractMibInstrumController

from ..domain.metric import Metric
from ..domain.oid import ObjectIdentifier
from ..domain.store import MetricStore
from ..domain.types import SnmpDataType

LOGGER = logging.getLogger(__name__)


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
        self._engine: engine.SnmpEngine | None = None
        self._transport: ManagedUdpTransport | None = None

    async def start(self) -> None:
        if self._engine is not None:
            raise RuntimeError("SNMP agent is already running")
        server_socket = bind_udp_socket(self.host, self.port)
        snmp_engine = engine.SnmpEngine()
        try:
            transport = ManagedUdpTransport()
            transport.open_server_mode(sock=server_socket)
            config.add_transport(
                snmp_engine,
                udp.DOMAIN_NAME,
                transport,
            )
            config.add_v1_system(snmp_engine, "emulator", self.community)
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
            cmdrsp.GetCommandResponder(snmp_engine, snmp_context)
            cmdrsp.NextCommandResponder(snmp_engine, snmp_context)
            cmdrsp.BulkCommandResponder(snmp_engine, snmp_context)
            await transport.wait_ready()
        except Exception:
            snmp_engine.close_dispatcher()
            server_socket.close()
            raise
        self._engine = snmp_engine
        self._transport = transport
        LOGGER.info("listening on udp://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._engine is not None:
            transport = self._transport
            self._engine.close_dispatcher()
            self._engine = None
            self._transport = None
            if transport is not None:
                await transport.wait_closed()
