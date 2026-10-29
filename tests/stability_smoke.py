from __future__ import annotations

import asyncio
import contextlib
import random
import socket

from installed_smoke import reserve_udp_port
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

from snmp_emulator.adapters.pysnmp_agent import SnmpAgent
from snmp_emulator.application import EmulatorApplication, build_store
from snmp_emulator.gui_controller import examples_directory
from snmp_emulator.json_config import load_json_config


async def verify_reply(client, target, operation, oid):
    args = (client, CommunityData("public", mpModel=1), target, ContextData())
    variable = ObjectType(ObjectIdentity(oid))
    if operation == "GETBULK":
        result = await bulk_cmd(*args, 0, 5, variable)
    else:
        command = get_cmd if operation == "GET" else next_cmd
        result = await command(*args, variable)
    error, status, _, values = result
    if error is not None or status or not values or int(values[0][1]) != 15:
        raise RuntimeError(f"stability {operation} failed: {error}, {status}, {values}")


async def malformed_traffic(port):
    generator = random.Random(20261029)
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.setblocking(False)
        for index in range(400):
            payload = generator.randbytes(generator.randrange(1, 513))
            await loop.sock_sendto(sender, payload, ("127.0.0.1", port))
            if index % 10 == 0:
                await asyncio.sleep(0)


async def main():
    examples = examples_directory()
    config = load_json_config(examples / "device.json", examples / "stable.json")
    port = reserve_udp_port()
    config = config.model_copy(
        update={"agent": config.agent.model_copy(update={"host": "127.0.0.1", "port": port})}
    )
    agent = SnmpAgent("127.0.0.1", port, "public", build_store(config))
    client = engine.SnmpEngine()
    loop = asyncio.get_running_loop()
    loop_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    await agent.start()
    traffic = asyncio.create_task(malformed_traffic(port))
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
        prefix = "1.3.6.1.4.1.55555"
        for index in range(600):
            operation = ("GET", "GETNEXT", "GETBULK")[index % 3]
            oid = config.metrics[0].oid if operation == "GET" else prefix
            await verify_reply(client, target, operation, oid)
        await traffic
        await agent.stop()
        for _ in range(20):
            application = EmulatorApplication(config)
            task = asyncio.create_task(application.run())
            try:
                await asyncio.wait_for(application.started.wait(), 3)
                try:
                    await application.run()
                except RuntimeError as exc:
                    if "already running" not in str(exc):
                        raise
                else:
                    raise RuntimeError("duplicate application run was accepted")
                await verify_reply(client, target, "GET", config.metrics[0].oid)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(("127.0.0.1", port))
        if loop_errors:
            raise RuntimeError(f"unhandled event loop errors: {loop_errors}")
    finally:
        traffic.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await traffic
        client.close_dispatcher()
        await agent.stop()
        loop.set_exception_handler(previous_handler)
    print("Stability: 620 successful requests, 400 malformed datagrams, 20 restart cycles")


if __name__ == "__main__":
    asyncio.run(main())
