from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
import sys
import sysconfig
from pathlib import Path

from pysnmp.entity import engine
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    UdpTransportTarget,
    get_cmd,
    next_cmd,
)
from pysnmp.smi import exval

from snmp_emulator.adapters.pysnmp_agent import SnmpAgent
from snmp_emulator.application import EmulatorApplication
from snmp_emulator.config import EmulatorConfig
from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType
from snmp_emulator.json_config import load_json_config

JSON_SCENARIOS = ("stable", "temperature-jump", "cpu-step")


async def smoke_json_scenario(examples: Path, name: str) -> None:
    config = load_json_config(examples / "device.json", examples / f"{name}.json")
    document = config.model_dump(mode="json")
    document["agent"].update(host="127.0.0.1", port=reserve_udp_port())
    document["duration"] /= 100
    document["update_interval"] /= 100
    for action in document["scenarios"][0]["actions"]:
        action["at"] /= 100
        if "interval" in action:
            action["interval"] /= 100
    config = EmulatorConfig.model_validate(document)
    application = EmulatorApplication(config)
    task = asyncio.create_task(application.run())
    client = engine.SnmpEngine()
    target = await UdpTransportTarget.create(("127.0.0.1", config.agent.port), timeout=1, retries=0)

    async def request(command, oid):
        error, status, _, values = await command(
            client,
            CommunityData(config.agent.community, mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        if error is not None or status:
            raise RuntimeError(f"JSON scenario SNMP request failed: {error}, {status}")
        return values[0]

    async def wait_started():
        while application.scenario_engine._started_at is None:
            if task.done():
                await task
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(wait_started(), timeout=3)
        await asyncio.sleep(config.duration + 0.05)
        if task.done():
            raise RuntimeError("application stopped after duration")
        expected = {item.oid: item.initial for item in config.metrics}
        if name == "temperature-jump":
            expected["1.3.6.1.4.1.55555.1.2.0"] = 80
        elif name == "cpu-step":
            expected["1.3.6.1.4.1.55555.1.1.0"] = 90
        for metric in config.metrics:
            _, value = await request(get_cmd, metric.oid)
            actual = (
                value.asOctets().decode("utf-8")
                if metric.type is SnmpDataType.OCTET_STRING
                else int(value)
            )
            if actual != expected[metric.oid]:
                raise RuntimeError(
                    f"{name}: {metric.name} expected {expected[metric.oid]}, got {actual}"
                )
        _, value = await request(get_cmd, "1.3.6.1.4.1.55555.99.0")
        if value.tagSet != exval.noSuchObject.tagSet:
            raise RuntimeError("unknown OID did not return noSuchObject")
        current_oid = "1.3.6.1.4.1.55555"
        for metric in sorted(config.metrics, key=lambda item: ObjectIdentifier.parse(item.oid)):
            oid, _ = await request(next_cmd, current_oid)
            if str(oid) != metric.oid:
                raise RuntimeError("JSON example walk returned an unexpected OID")
            current_oid = str(oid)
        _, value = await request(next_cmd, current_oid)
        if value.tagSet != exval.endOfMibView.tagSet:
            raise RuntimeError("walk did not finish with endOfMibView")
        await asyncio.sleep(config.update_interval * 2)
        for metric in application.store.snapshot():
            if metric.value != expected[str(metric.oid)]:
                raise RuntimeError("scenario changed values after duration")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        client.close_dispatcher()

    restarted = EmulatorApplication(config)
    await restarted.agent.start()
    client = engine.SnmpEngine()
    try:
        for metric in config.metrics:
            _, value = await request(get_cmd, metric.oid)
            actual = (
                value.asOctets().decode("utf-8")
                if metric.type is SnmpDataType.OCTET_STRING
                else int(value)
            )
            if actual != metric.initial:
                raise RuntimeError("restart did not restore initial values")
    finally:
        client.close_dispatcher()
        await restarted.agent.stop()


async def smoke_installed_json() -> None:
    examples = Path(sysconfig.get_path("data")) / "share" / "snmp-v2c-emulator" / "examples"
    for name in JSON_SCENARIOS:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "snmp_emulator",
                "--device-config",
                str(examples / "device.json"),
                "--scenario",
                str(examples / f"{name}.json"),
                "--check-config",
            ],
            check=True,
        )
        await smoke_json_scenario(examples, name)


def reserve_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def smoke_installed_agent() -> None:
    port = reserve_udp_port()
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("smoke", oid, SnmpDataType.INTEGER, 42)])
    agent = SnmpAgent("127.0.0.1", port, "public", store)
    client_engine = engine.SnmpEngine()
    await agent.start()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
        error, status, _, var_binds = await get_cmd(
            client_engine,
            CommunityData("public", mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(str(oid))),
        )
        if error is not None or status or len(var_binds) != 1 or int(var_binds[0][1]) != 42:
            raise RuntimeError(
                "installed wheel did not answer the SNMP v2c smoke request: "
                f"error={error!r}, status={status!r}, var_binds={var_binds!r}"
            )
    finally:
        client_engine.close_dispatcher()
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(smoke_installed_agent())
    asyncio.run(smoke_installed_json())
