from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import time
import tkinter as tk
from pathlib import Path

from installed_smoke import reserve_udp_port
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

from snmp_emulator.gui import EmulatorPanel
from snmp_emulator.gui_controller import examples_directory


def pump(root, predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("GUI smoke timed out")
        root.update()
        time.sleep(0.01)
    root.update()


async def verify_snmp(snapshot, port):
    client = engine.SnmpEngine()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
        previous = "1.3.6.1.4.1.55555"
        for metric in snapshot.metrics:
            for command, oid in ((get_cmd, str(metric.oid)), (next_cmd, previous)):
                error, status, _, bindings = await command(
                    client,
                    CommunityData("public", mpModel=1),
                    target,
                    ContextData(),
                    ObjectType(ObjectIdentity(oid)),
                )
                if error or status or str(bindings[0][0]) != str(metric.oid):
                    raise RuntimeError(f"GUI SNMP request failed: {error}, {status}")
                if bindings[0][1].prettyPrint() != str(metric.value):
                    raise RuntimeError("GUI and SNMP values differ")
            previous = str(metric.oid)
    finally:
        client.close_dispatcher()


def main():
    root = tk.Tk()
    window_closed = False

    def on_destroy(event):
        nonlocal window_closed
        if event.widget is root:
            window_closed = True

    root.bind("<Destroy>", on_destroy, add=True)
    panel = EmulatorPanel(root)
    try:
        with tempfile.TemporaryDirectory() as directory:
            examples = examples_directory()
            device = json.loads((examples / "device.json").read_text())
            port = reserve_udp_port()
            device["agent"]["port"] = port
            scenario = json.loads((examples / "cpu-step.json").read_text())
            scenario.update(duration=0.5, update_interval=0.05)
            scenario["rules"][0]["step"] = 30
            device_path, scenario_path = (
                Path(directory) / "device.json",
                Path(directory) / "scenario.json",
            )
            device_path.write_text(json.dumps(device), encoding="utf-8")
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            panel.device.set(str(device_path))
            panel.scenario.set(str(scenario_path))
            panel.mode.set("custom")
            panel._change_mode()
            panel.start()
            panel.start()
            pump(root, lambda: panel.controller.snapshot().state == "completed")
            snapshot = panel.controller.snapshot()
            if snapshot.metrics[0].value != 90:
                raise RuntimeError("GUI scenario did not reach limit")
            asyncio.run(verify_snmp(snapshot, port))
            pump(root, lambda: len(panel.table.get_children()) == 5)
            panel.stop()
            pump(root, lambda: not panel.controller.active)
            panel.start()
            pump(root, lambda: panel.controller.snapshot().state == "running")
            panel.close()
            pump(root, lambda: not panel.controller.active)
            with socket.socket(type=socket.SOCK_DGRAM) as probe:
                probe.bind(("127.0.0.1", port))
    finally:
        panel.controller.stop()
        pump(root, lambda: not panel.controller.active)
        if not window_closed:
            root.destroy()
    print("GUI, lifecycle and external SNMP GET/WALK passed")


if __name__ == "__main__":
    main()
