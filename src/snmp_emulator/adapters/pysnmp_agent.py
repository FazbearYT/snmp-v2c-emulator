from __future__ import annotations

import asyncio
import logging
from typing import Any

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto import rfc1902
from pysnmp.proto.api import v2c
from pysnmp.smi import exval
from pysnmp.smi.instrum import AbstractMibInstrumController

from ..domain.metric import Metric
from ..domain.oid import ObjectIdentifier
from ..domain.store import MetricStore
from ..domain.types import SnmpDataType


LOGGER = logging.getLogger(__name__)


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

    def read_variables(self, *var_binds: tuple[Any, Any], **context_data: Any) -> list[tuple[Any, Any]]:
        result: list[tuple[Any, Any]] = []
        for name, _ in var_binds:
            oid = ObjectIdentifier(tuple(int(part) for part in name))
            metric = self.store.get(oid)
            result.append((name, encode_value(metric) if metric else exval.noSuchObject))
        return result


class SnmpAgent:
    def __init__(self, host: str, port: int, community: str, store: MetricStore) -> None:
        self.host = host
        self.port = port
        self.community = community
        self.store = store
        self._engine: engine.SnmpEngine | None = None

    async def start(self) -> None:
        snmp_engine = engine.SnmpEngine()
        config.add_transport(
            snmp_engine,
            udp.DOMAIN_NAME,
            udp.UdpAsyncioTransport().open_server_mode((self.host, self.port)),
        )
        config.add_v1_system(snmp_engine, "emulator", self.community)
        config.add_vacm_user(
            snmp_engine,
            2,
            "emulator",
            "noAuthNoPriv",
            (1, 3, 6),
            (1, 3, 6),
        )
        snmp_context = context.SnmpContext(snmp_engine)
        snmp_context.unregister_context_name(b"")
        snmp_context.register_context_name(b"", StoreInstrumentation(self.store))
        cmdrsp.GetCommandResponder(snmp_engine, snmp_context)
        self._engine = snmp_engine
        await asyncio.sleep(0)
        LOGGER.info("listening on udp://%s:%d", self.host, self.port)
        # TODO: register GETNEXT and GETBULK responders after exact lookup is covered.

    async def stop(self) -> None:
        if self._engine is not None:
            self._engine.close_dispatcher()
            self._engine = None
