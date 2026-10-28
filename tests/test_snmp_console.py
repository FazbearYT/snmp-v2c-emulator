import asyncio
import contextlib
import queue
import time
from pathlib import Path

import pytest
from installed_smoke import reserve_udp_port

from snmp_emulator import snmp_console
from snmp_emulator.application import EmulatorApplication, build_store
from snmp_emulator.json_config import load_json_config
from snmp_emulator.snmp_console import (
    ConsoleClient,
    Request,
    parse_request,
    subtree_root,
    suggestions,
)

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.fixture
def metrics():
    return build_store(
        load_json_config(EXAMPLES / "device.json", EXAMPLES / "stable.json")
    ).snapshot()


@pytest.mark.parametrize(
    "command,operation,oid",
    [
        ("get cpu_usage", "GET", "1.3.6.1.4.1.55555.1.1.0"),
        ("GETNEXT .01.3.6", "GETNEXT", "1.3.6"),
        ("getwalk 1.3.6", "WALK", "1.3.6"),
        ("snmpwalk 1.3.6", "WALK", "1.3.6"),
        ("getbulk 1.3.6 8", "GETBULK", "1.3.6"),
    ],
)
def test_parse_command(command, operation, oid, metrics):
    request = parse_request(command, metrics)
    assert request.operation == operation
    assert request.oid == oid
    assert request.repetitions == (8 if operation == "GETBULK" else 5)


@pytest.mark.parametrize(
    "command",
    [
        "",
        "whoami",
        "GET",
        "GET 1.3 2",
        "GETBULK 1.3 zero",
        "GETBULK 1.3 0",
        "GETBULK 1.3 51",
        "GET missing",
        "WALK 9.9",
        "GET 1.3;shutdown",
    ],
)
def test_invalid_commands_are_rejected(command, metrics):
    with pytest.raises(ValueError):
        parse_request(command, metrics)


def test_context_completion_uses_loaded_metrics(metrics):
    assert suggestions("GETN", metrics) == [("GETNEXT", "GETNEXT ")]
    assert suggestions("GET cpu", metrics) == [
        ("cpu_usage · 1.3.6.1.4.1.55555.1.1.0", "GET 1.3.6.1.4.1.55555.1.1.0")
    ]
    assert len(suggestions("GET 1.3.6.1.4.1.55555.1.", metrics)) == 5
    assert suggestions("GETBULK 1.3 5", metrics) == []
    assert suggestions("GET arbitrary", metrics) == []
    assert subtree_root(metrics) == "1.3.6.1.4.1.55555.1"
    assert subtree_root(metrics[:1]) == "1.3.6.1.4.1.55555.1.1"
    assert subtree_root(()) == "1.3.6.1"


@pytest.fixture
async def running_application():
    config = load_json_config(EXAMPLES / "device.json", EXAMPLES / "stable.json")
    config = config.model_copy(
        update={"agent": config.agent.model_copy(update={"port": reserve_udp_port()})}
    )
    application = EmulatorApplication(config)
    task = asyncio.create_task(application.run())
    await asyncio.wait_for(application.started.wait(), 3)
    try:
        yield application
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def output_when_done(client):
    deadline = time.monotonic() + 5
    while client.active:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)
    lines = []
    while True:
        try:
            lines.append(client.output.get_nowait())
        except queue.Empty:
            return "\n".join(lines)


@pytest.mark.parametrize(
    "operation,expected_count", [("GET", 1), ("GETNEXT", 1), ("GETBULK", 3), ("WALK", 5)]
)
async def test_real_udp_requests(operation, expected_count, running_application, monkeypatch):
    application = running_application
    calls = []
    attribute = {
        "GET": "get_cmd",
        "GETNEXT": "next_cmd",
        "WALK": "next_cmd",
        "GETBULK": "bulk_cmd",
    }[operation]
    original = getattr(snmp_console, attribute)

    async def record(*args, **kwargs):
        calls.append(attribute)
        return await original(*args, **kwargs)

    monkeypatch.setattr(snmp_console, attribute, record)
    metrics = application.store.snapshot()
    oid = str(metrics[0].oid) if operation == "GET" else subtree_root(metrics)
    client = ConsoleClient()
    assert client.start(
        Request(operation, oid, 3), "127.0.0.1", application.config.agent.port, "public"
    )
    assert not client.start(
        Request("GET", oid), "127.0.0.1", application.config.agent.port, "public"
    )
    output = await output_when_done(client)
    assert "Ошибка" not in output
    assert output.count(" = ") == expected_count
    assert len(calls) == (6 if operation == "WALK" else 1)
    assert "Integer: 15" in output or "Integer32: 15" in output
    if operation == "WALK":
        assert "получено метрик: 5" in output


async def test_unknown_oid_and_end_of_mib(running_application):
    client = ConsoleClient()
    port = running_application.config.agent.port
    client.start(Request("GET", "1.3.6.1.4.1.55555.99.0"), "127.0.0.1", port, "public")
    assert "NoSuchObject" in await output_when_done(client)
    client.start(Request("GETNEXT", "2.999"), "127.0.0.1", port, "public")
    assert "EndOfMibView" in await output_when_done(client)


async def test_timeout_is_reported():
    client = ConsoleClient()
    client.start(Request("GET", "1.3.6.1"), "127.0.0.1", reserve_udp_port(), "public")
    assert "Ошибка запроса" in await output_when_done(client)


async def test_cancel_releases_client_and_allows_retry(running_application):
    client = ConsoleClient()
    request = Request("GET", "1.3.6.1.4.1.55555.1.1.0")
    client.start(request, "127.0.0.1", reserve_udp_port(), "public")
    client.cancel()
    assert "Запрос отменён" in await output_when_done(client)
    client.start(request, "0.0.0.0", running_application.config.agent.port, "public")
    assert "Ошибка" not in await output_when_done(client)


async def test_walk_stays_inside_requested_subtree(running_application):
    client = ConsoleClient()
    client.start(
        Request("WALK", "1.3.6.1.4.1.55555.1.2"),
        "127.0.0.1",
        running_application.config.agent.port,
        "public",
    )
    output = await output_when_done(client)
    assert output.count(" = ") == 1
    assert "1.3.6.1.4.1.55555.1.2.0 = " in output
    assert "получено метрик: 1" in output
