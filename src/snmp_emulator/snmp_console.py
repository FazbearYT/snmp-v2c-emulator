from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import dataclass

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
from pysnmp.proto import rfc1905

from .domain.metric import Metric
from .domain.oid import ObjectIdentifier

COMMANDS = {
    "GET": "GET <OID или имя> — значение конкретной метрики",
    "GETNEXT": "GETNEXT <OID или имя> — следующая метрика по порядку OID",
    "WALK": "WALK <корень OID> — обход ветки последовательными GETNEXT",
    "GETBULK": "GETBULK <OID> [количество 1–50] — следующие значения одним запросом",
}
ALIASES = {
    "GETWALK": "WALK",
    "SNMPWALK": "WALK",
    "SNMPGET": "GET",
    "SNMPGETNEXT": "GETNEXT",
    "SNMPBULKGET": "GETBULK",
}


@dataclass(frozen=True)
class Request:
    operation: str
    oid: str
    repetitions: int = 5


def parse_request(text: str, metrics: tuple[Metric, ...]) -> Request:
    parts = text.split()
    if not parts:
        raise ValueError("Введите команду или выберите шаблон")
    operation = ALIASES.get(parts[0].upper(), parts[0].upper())
    if operation not in COMMANDS:
        raise ValueError("Доступные команды: GET, GETNEXT, WALK, GETBULK")
    if len(parts) not in ((2, 3) if operation == "GETBULK" else (2,)):
        raise ValueError(COMMANDS[operation])
    names = {metric.name: str(metric.oid) for metric in metrics}
    oid = str(ObjectIdentifier.parse(names.get(parts[1], parts[1])))
    repetitions = 5
    if len(parts) == 3:
        try:
            repetitions = int(parts[2])
        except ValueError as exc:
            raise ValueError("Количество GETBULK должно быть целым числом от 1 до 50") from exc
        if not 1 <= repetitions <= 50:
            raise ValueError("Количество GETBULK должно быть от 1 до 50")
    return Request(operation, oid, repetitions)


def subtree_root(metrics: tuple[Metric, ...]) -> str:
    if not metrics:
        return "1.3.6.1"
    prefix = list(metrics[0].oid.parts)
    for metric in metrics[1:]:
        while tuple(prefix) != metric.oid.parts[: len(prefix)]:
            prefix.pop()
    if any(tuple(prefix) == metric.oid.parts for metric in metrics):
        prefix = prefix[:-1]
    return ".".join(map(str, prefix if len(prefix) >= 2 else metrics[0].oid.parts[:2]))


def suggestions(text: str, metrics: tuple[Metric, ...]) -> list[tuple[str, str]]:
    parts = text.split()
    if len(parts) < 2 and not text.endswith(" "):
        prefix = text.upper()
        return [(command, command + " ") for command in COMMANDS if command.startswith(prefix)]
    operation = ALIASES.get(parts[0].upper(), parts[0].upper()) if parts else ""
    if operation not in COMMANDS or len(parts) > 2:
        return []
    token = parts[1] if len(parts) == 2 else ""
    candidates = [(metric.name, str(metric.oid)) for metric in metrics]
    if operation != "GET":
        candidates.insert(0, ("Ветка устройства", subtree_root(metrics)))
    return [
        (f"{name} · {oid}", f"{operation} {oid}")
        for name, oid in candidates
        if not token or oid.startswith(token.lstrip(".")) or token.casefold() in name.casefold()
    ][:12]


class ConsoleClient:
    def __init__(self) -> None:
        self.output: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, request: Request, host: str, port: int, community: str) -> bool:
        if self.active:
            return False
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, args=(request, host, port, community))
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self, request: Request, host: str, port: int, community: str) -> None:
        host = "127.0.0.1" if host == "0.0.0.0" else host
        self.output.put(
            f"> {request.operation} {request.oid}"
            + (f" {request.repetitions}" if request.operation == "GETBULK" else "")
        )
        self.output.put(f"SNMP v2c · UDP {host}:{port}")
        started_at = time.monotonic()
        try:
            asyncio.run(self._request(request, host, port, community))
        except TimeoutError:
            self.output.put("Ошибка запроса: превышено общее время выполнения (15 с)")
        except Exception as exc:
            self.output.put(f"Ошибка запроса: {exc}")
        finally:
            self.output.put(f"Завершено за {time.monotonic() - started_at:.3f} с\n")

    async def _request(self, request: Request, host: str, port: int, community: str) -> None:
        client = engine.SnmpEngine()
        try:
            async with asyncio.timeout(15):
                target = await UdpTransportTarget.create((host, port), timeout=1, retries=0)
                current = ObjectIdentifier.parse(request.oid).parts
                root = current
                count = 0
                while not self._cancel.is_set():
                    args = (client, CommunityData(community, mpModel=1), target, ContextData())
                    variable = ObjectType(ObjectIdentity(current))
                    if request.operation == "GETBULK":
                        result = await bulk_cmd(*args, 0, request.repetitions, variable)
                    else:
                        command = get_cmd if request.operation == "GET" else next_cmd
                        result = await command(*args, variable)
                    if self._cancel.is_set():
                        break
                    error, status, index, bindings = result
                    if error:
                        raise RuntimeError(str(error))
                    if status:
                        raise RuntimeError(f"{status.prettyPrint()} (индекс {index})")
                    if not bindings:
                        self.output.put("Пустой ответ")
                        return
                    for oid, value in bindings:
                        next_oid = tuple(oid)
                        terminal = isinstance(
                            value,
                            (rfc1905.EndOfMibView, rfc1905.NoSuchObject, rfc1905.NoSuchInstance),
                        )
                        if request.operation == "WALK":
                            if terminal or next_oid[: len(root)] != root:
                                self.output.put(f"Конец ветки · получено метрик: {count}")
                                return
                            if next_oid <= current:
                                raise RuntimeError("OID ответа не увеличивается; обход остановлен")
                        self.output.put(f"{oid} = {type(value).__name__}: {value.prettyPrint()}")
                        current = next_oid
                        count += 1
                    if request.operation != "WALK":
                        return
                    if count >= 256:
                        self.output.put("Достигнут предел обхода: 256 метрик")
                        return
                self.output.put("Запрос отменён")
        finally:
            client.close_dispatcher()
