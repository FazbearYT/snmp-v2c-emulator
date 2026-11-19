import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import pytest
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
from pysnmp.smi import exval

from snmp_emulator import gui_controller
from snmp_emulator.gui_controller import PanelController, examples_directory

EXAMPLES = Path(__file__).parents[1] / "examples"


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for panel state")
        time.sleep(0.01)


@pytest.fixture
def controller():
    panel = PanelController()
    yield panel
    panel.stop()
    wait_until(lambda: not panel.active)


@pytest.fixture
def profile(tmp_path):
    device = json.loads((EXAMPLES / "device.json").read_text())
    port = reserve_udp_port()
    device["agent"]["port"] = port
    device_path = tmp_path / "device.json"
    device_path.write_text(json.dumps(device), encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(
        json.dumps(
            {
                "duration": 0.2,
                "update_interval": 0.05,
                "rules": [
                    {"type": "step", "oid": device["metrics"][0]["oid"], "step": 10, "limit": 45}
                ],
            }
        ),
        encoding="utf-8",
    )
    return device_path, scenario_path, port


def test_examples_available():
    assert (examples_directory() / "cpu-step.json").is_file()


def test_installed_examples_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(gui_controller, "__file__", str(tmp_path / "package" / "controller.py"))
    monkeypatch.setattr(gui_controller.sysconfig, "get_path", lambda _: str(tmp_path))
    assert examples_directory() == tmp_path / "share" / "snmp-v2c-emulator" / "examples"


def test_stop_restart_and_initial_reset(controller, profile):
    device, scenario, port = profile
    assert controller.start(device, scenario)
    assert not controller.start(device, scenario)
    wait_until(lambda: controller.snapshot().state == "completed")
    assert controller.snapshot().metrics[0].value == 45
    assert controller.active
    controller.stop()
    controller.stop()
    wait_until(lambda: not controller.active)
    assert controller.snapshot().state == "stopped"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", port))
    document = json.loads(scenario.read_text())
    document["duration"] = 5
    document["rules"] = [{"type": "constant", "oid": "1.3.6.1.4.1.55555.1.1.0", "value": 15}]
    scenario.write_text(json.dumps(document), encoding="utf-8")
    assert controller.start(device, scenario)
    wait_until(lambda: controller.snapshot().state == "running")
    assert controller.snapshot().metrics[0].value == 15


def test_configuration_error_and_recovery(controller, profile):
    device, scenario, _ = profile
    controller.start(device.with_name("missing.json"), scenario)
    wait_until(lambda: not controller.active)
    assert controller.snapshot().state == "error"
    assert "missing.json" in controller.snapshot().error
    controller.start(device, scenario)
    wait_until(lambda: controller.snapshot().state == "running")
    assert not controller.snapshot().error


def test_busy_port(controller, profile):
    device, scenario, port = profile
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        occupied.bind(("127.0.0.1", port))
        controller.start(device, scenario)
        wait_until(lambda: not controller.active)
    assert controller.snapshot().state == "error"
    assert "cannot bind" in controller.snapshot().error
    assert str(port) in controller.snapshot().error


def test_stop_during_loading_prevents_start(controller, profile, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = gui_controller.load_json_config

    def delayed_load(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(gui_controller, "load_json_config", delayed_load)
    controller.start(*profile[:2])
    try:
        assert entered.wait(3)
        controller.stop()
    finally:
        release.set()
    wait_until(lambda: not controller.active)
    assert controller.snapshot().state == "stopped"
    assert not controller.snapshot().metrics


def test_background_failure_is_visible(controller, profile, monkeypatch):
    async def fail(_self):
        raise RuntimeError("scenario failed")

    monkeypatch.setattr(gui_controller.EmulatorApplication, "run", fail)
    controller.start(*profile[:2])
    wait_until(lambda: not controller.active)
    assert controller.snapshot().state == "error"
    assert "scenario failed" in controller.snapshot().error


@pytest.mark.asyncio
async def test_panel_values_match_external_get_and_walk(controller, profile):
    device, scenario, port = profile
    controller.start(device, scenario)
    await asyncio.to_thread(wait_until, lambda: controller.snapshot().state == "completed")
    snapshot = controller.snapshot()
    client = engine.SnmpEngine()
    try:
        target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)

        async def request(command, oid):
            error, status, _, bindings = await command(
                client,
                CommunityData("public", mpModel=1),
                target,
                ContextData(),
                ObjectType(ObjectIdentity(oid)),
            )
            assert error is None
            assert not status
            return bindings[0]

        for metric in snapshot.metrics:
            _, value = await request(get_cmd, str(metric.oid))
            assert value.prettyPrint() == str(metric.value)
        oid = "1.3.6.1.4.1.55555"
        for metric in snapshot.metrics:
            next_oid, value = await request(next_cmd, oid)
            assert str(next_oid) == str(metric.oid)
            assert value.prettyPrint() == str(metric.value)
            oid = str(next_oid)
        _, value = await request(next_cmd, oid)
        assert value.tagSet == exval.endOfMibView.tagSet
    finally:
        client.close_dispatcher()
