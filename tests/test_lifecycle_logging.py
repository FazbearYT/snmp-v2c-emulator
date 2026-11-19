import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from installed_smoke import reserve_udp_port

from snmp_emulator.application import EmulatorApplication
from snmp_emulator.config import load_config
from snmp_emulator.gui_controller import PanelController
from snmp_emulator.json_config import load_json_config

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.fixture(params=["json", "yaml"])
def profile(request, tmp_path):
    port = reserve_udp_port()
    if request.param == "yaml":
        document = yaml.safe_load((EXAMPLES / "device.yaml").read_text(encoding="utf-8"))
        document["agent"].update(host="127.0.0.1", port=port)
        document["scenarios"] = []
        path = tmp_path / "device.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return load_config(path), ["--config", str(path)]
    document = json.loads((EXAMPLES / "device.json").read_text(encoding="utf-8"))
    document["agent"]["port"] = port
    device = tmp_path / "device.json"
    device.write_text(json.dumps(document), encoding="utf-8")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "duration": 0.1,
                "update_interval": 0.05,
                "rules": [
                    {
                        "type": "constant",
                        "oid": document["metrics"][0]["oid"],
                        "value": 20,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return load_json_config(device, scenario), [
        "--device-config",
        str(device),
        "--scenario",
        str(scenario),
    ]


def lifecycle_messages(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "snmp_emulator.application"
    ]


async def cancel_application(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_start_stop_and_restart_are_logged_once(profile, caplog):
    config, _ = profile
    caplog.set_level(logging.INFO)
    for attempt in range(2):
        application = EmulatorApplication(config)
        task = asyncio.create_task(application.run())
        try:
            await asyncio.wait_for(application.started.wait(), 3)
            with pytest.raises(RuntimeError, match="already running"):
                await application.run()
        finally:
            await cancel_application(task)
        await application.agent.stop()
        assert lifecycle_messages(caplog) == [
            f"listening on udp://127.0.0.1:{config.agent.port}",
            "emulator stopped",
        ] * (attempt + 1)


async def test_failed_bind_has_no_successful_lifecycle_records(profile, caplog):
    config, _ = profile
    caplog.set_level(logging.INFO)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        occupied.bind(("127.0.0.1", config.agent.port))
        application = EmulatorApplication(config)
        with pytest.raises(OSError, match="cannot bind"):
            await application.run()
        assert lifecycle_messages(caplog) == []
    task = asyncio.create_task(application.run())
    try:
        await asyncio.wait_for(application.started.wait(), 3)
    finally:
        await cancel_application(task)
    assert lifecycle_messages(caplog) == [
        f"listening on udp://127.0.0.1:{config.agent.port}",
        "emulator stopped",
    ]


async def test_cancelled_start_has_no_successful_lifecycle_records(profile, caplog):
    config, _ = profile
    application = EmulatorApplication(config)
    entered = asyncio.Event()

    class PausedAgent:
        async def start(self):
            entered.set()
            await asyncio.Event().wait()

        async def stop(self):
            pass

    application.agent = PausedAgent()
    caplog.set_level(logging.INFO)
    task = asyncio.create_task(application.run())
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        await cancel_application(task)
    assert lifecycle_messages(caplog) == []


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "Timed out waiting for emulator"
        time.sleep(0.01)


def test_gui_logs_metric_changes_and_lifecycle_on_each_run(tmp_path, caplog):
    document = json.loads((EXAMPLES / "device.json").read_text(encoding="utf-8"))
    document["agent"]["port"] = reserve_udp_port()
    device = tmp_path / "device.json"
    device.write_text(json.dumps(document), encoding="utf-8")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "duration": 0.1,
                "update_interval": 0.05,
                "rules": [{"type": "constant", "oid": document["metrics"][0]["oid"], "value": 20}],
            }
        ),
        encoding="utf-8",
    )
    panel = PanelController()
    caplog.set_level(logging.INFO)
    try:
        for attempt in range(2):
            assert panel.start(device, scenario)
            assert not panel.start(device, scenario)
            wait_until(lambda: panel.snapshot().state == "completed")
            assert panel.active
            assert lifecycle_messages(caplog).count("emulator stopped") == attempt
            panel.stop()
            panel.stop()
            wait_until(lambda: not panel.active)
            assert lifecycle_messages(caplog) == [
                f"listening on udp://127.0.0.1:{document['agent']['port']}",
                "emulator stopped",
            ] * (attempt + 1)
            changes = [
                record.getMessage()
                for record in caplog.records
                if record.name == "snmp_emulator.scenarios.engine"
            ]
            assert len(changes) == attempt + 1
            assert all("15 -> 20" in message for message in changes)
    finally:
        panel.stop()
        wait_until(lambda: not panel.active)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process signals")
@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_cli_signals_log_one_start_and_stop(profile, tmp_path, stop_signal):
    config, arguments = profile
    log_path = tmp_path / "cli.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "snmp_emulator", *arguments],
            env={**os.environ, "PYTHONPATH": str(EXAMPLES.parent / "src")},
            stdout=log,
            stderr=log,
        )
        try:

            def started():
                assert process.poll() is None, log_path.read_text(encoding="utf-8")
                return "listening on" in log_path.read_text(encoding="utf-8")

            wait_until(started)
            process.send_signal(stop_signal)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    output = log_path.read_text(encoding="utf-8")
    assert process.returncode == 0
    assert output.count(f"listening on udp://127.0.0.1:{config.agent.port}") == 1
    assert output.count("emulator stopped") == 1
    assert "Traceback" not in output
