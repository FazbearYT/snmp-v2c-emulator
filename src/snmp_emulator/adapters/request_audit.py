"""Request/response audit at the UDP boundary shared by CLI and GUI.

Only authenticated, supported SNMPv2c requests enter the audit. The community
is intentionally neither stored nor written to the log.
"""
from __future__ import annotations

import logging
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from pyasn1.codec.ber import decoder
from pysnmp.proto.api import v2c
from pysnmp.smi import exval

LOGGER = logging.getLogger(__name__)
MAX_PENDING_REQUESTS = 4096


@dataclass(frozen=True)
class RequestRecord:
    timestamp: str
    address: str
    port: int
    pdu_type: str
    request_id: int
    oids: tuple[str, ...]


class RequestAudit:
    """Correlate datagrams by source and request ID, including retransmissions.

    Each received datagram gets its own monotonically increasing internal token;
    duplicates with identical request IDs are never collapsed.
    """

    def __init__(self, community: bytes, message_spec: Callable[[], Any]) -> None:
        self._community = community
        self._message_spec = message_spec
        self._next_token = 0
        self._pending: OrderedDict[int, RequestRecord] = OrderedDict()
        self._by_key: dict[tuple[str, int, int], deque[int]] = {}

    @staticmethod
    def _key(address: Any, request_id: int) -> tuple[str, int, int]:
        return (str(address[0]), int(address[1]), request_id)

    def _parse(self, datagram: bytes) -> Any | None:
        try:
            message, trailing = decoder.decode(datagram, asn1Spec=self._message_spec())
            if trailing or int(v2c.apiMessage.get_version(message)) != 1:
                return None
            return message
        except Exception:
            return None

    def received(self, datagram: bytes, address: Any) -> None:
        message = self._parse(datagram)
        if message is None:
            return
        try:
            if bytes(v2c.apiMessage.get_community(message)) != self._community:
                return
            pdu = v2c.apiMessage.get_pdu(message)
            pdu_types = (
                (v2c.GetRequestPDU.tagSet, "GET"),
                (v2c.GetNextRequestPDU.tagSet, "GETNEXT"),
                (v2c.GetBulkRequestPDU.tagSet, "GETBULK"),
            )
            pdu_type = next((name for tag, name in pdu_types if pdu.tagSet == tag), None)
            if pdu_type is None:
                return
            request_id = int(v2c.apiPDU.get_request_id(pdu))
            oids = tuple(str(oid) for oid, _ in v2c.apiPDU.get_varbinds(pdu))
            record = RequestRecord(
                datetime.now(UTC).isoformat(),
                str(address[0]),
                int(address[1]),
                pdu_type,
                request_id,
                oids,
            )
        except Exception:
            return
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            oldest = next(iter(self._pending))
            self._finish(oldest, "not_sent", reason="pending_limit")
        self._next_token += 1
        token = self._next_token
        self._pending[token] = record
        self._by_key.setdefault(self._key(address, request_id), deque()).append(token)
        LOGGER.info(
            "snmp_request event=received token=%d time=%s source=%s:%d "
            "pdu=%s request_id=%d oids=%s",
            token, record.timestamp, record.address, record.port,
            record.pdu_type, record.request_id, ",".join(record.oids),
        )

    def _finish(
        self,
        token: int,
        outcome: str,
        *,
        error_status: int | None = None,
        error_index: int | None = None,
        exceptions: tuple[str, ...] = (),
        reason: str | None = None,
    ) -> None:
        record = self._pending.pop(token, None)
        if record is None:
            return
        key = self._key((record.address, record.port), record.request_id)
        queue = self._by_key.get(key)
        if queue is not None:
            try:
                queue.remove(token)
            except ValueError:
                pass
            if not queue:
                del self._by_key[key]
        LOGGER.info(
            "snmp_request event=result token=%d source=%s:%d pdu=%s "
            "request_id=%d outcome=%s error_status=%s error_index=%s "
            "exceptions=%s reason=%s",
            token, record.address, record.port, record.pdu_type,
            record.request_id, outcome,
            error_status if error_status is not None else "-",
            error_index if error_index is not None else "-",
            ",".join(exceptions) if exceptions else "-",
            reason if reason is not None else "-",
        )

    def response(self, datagram: bytes, address: Any, *, sent: bool) -> None:
        message = self._parse(datagram)
        if message is None:
            return
        try:
            pdu = v2c.apiMessage.get_pdu(message)
            if pdu.tagSet != v2c.ResponsePDU.tagSet:
                return
            request_id = int(v2c.apiPDU.get_request_id(pdu))
            key = self._key(address, request_id)
            queue = self._by_key.get(key)
            if not queue:
                return
            token = queue[0]
            status = int(v2c.apiPDU.get_error_status(pdu))
            index = int(v2c.apiPDU.get_error_index(pdu))
            exceptions = tuple(
                f"{oid}:{name}"
                for oid, value in v2c.apiPDU.get_varbinds(pdu)
                for tag, name in (
                    (exval.noSuchObject.tagSet, "noSuchObject"),
                    (exval.noSuchInstance.tagSet, "noSuchInstance"),
                    (exval.endOfMibView.tagSet, "endOfMibView"),
                )
                if value.tagSet == tag
            )
        except Exception:
            return
        self._finish(
            token,
            "sent" if sent else "not_sent",
            error_status=status,
            error_index=index,
            exceptions=exceptions,
            reason=None if sent else "transport_send_failed",
        )

    def close(self) -> None:
        for token in list(self._pending):
            self._finish(token, "not_sent", reason="agent_stopped")
