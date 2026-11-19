import json
import logging
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from snmp_emulator import gui_controller
from snmp_emulator.gui_controller import PanelController

PROJECT = Path(__file__).parents[1]
EXAMPLES = PROJECT / "examples"
TIMESTAMP = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"


def run_python(*arguments):
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=PROJECT,
        env={**os.environ, "PYTHONPATH": str(PROJECT / "src")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_log_level_format_and_repeated_configuration(level):
    result = run_python(
        "-c",
        """
import logging
import sys
from snmp_emulator.logging_config import configure_logging

configure_logging('ERROR')
configure_logging(sys.argv[1])
logger = logging.getLogger('snmp_emulator.test')
logger.debug('debug event')
logger.info('info event')
logger.warning('warning event')
logger.error('error event')
""",
        level,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    selected = ("DEBUG", "INFO", "WARNING", "ERROR")
    expected = selected[selected.index(level) :]
    lines = result.stderr.splitlines()
    assert len(lines) == len(expected)
    for line, name in zip(lines, expected, strict=True):
        assert re.fullmatch(rf"{TIMESTAMP} {name} snmp_emulator.test: {name.lower()} event", line)


def test_configuration_preserves_existing_handler_and_updates_level():
    result = run_python(
        "-c",
        """
import logging
from snmp_emulator.logging_config import configure_logging

handler = logging.StreamHandler()
formatter = logging.Formatter('existing: %(message)s')
handler.setFormatter(formatter)
root = logging.getLogger()
root.addHandler(handler)
configure_logging('ERROR')
configure_logging('INFO')
assert root.handlers == [handler]
assert handler.formatter is formatter
logging.getLogger('snmp_emulator.test').info('preserved')
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == "existing: preserved\n"


def test_logging_without_console_does_not_crash():
    result = run_python(
        "-c",
        """
import logging
import sys
from snmp_emulator.logging_config import configure_logging

sys.stderr = None
configure_logging()
logging.getLogger('snmp_emulator.test').error('no attached console')
print('still running')
""",
    )

    assert result.returncode == 0
    assert result.stdout == "still running\n"
    assert result.stderr == ""


def invalid_configuration(tmp_path, file_format, category):
    device_path = tmp_path / f"device.{file_format}"
    scenario_path = tmp_path / "scenario.json"
    if file_format == "json":
        document = json.loads((EXAMPLES / "device.json").read_text(encoding="utf-8"))
        scenario = json.loads((EXAMPLES / "stable.json").read_text(encoding="utf-8"))
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    else:
        document = yaml.safe_load((EXAMPLES / "device.yaml").read_text(encoding="utf-8"))

    if category == "read":
        return device_path, scenario_path, "cannot read"
    if category == "encoding":
        device_path.write_bytes(b"\xff")
        return device_path, scenario_path, "cannot decode"
    if category == "parse":
        device_path.write_text("[", encoding="utf-8")
        return device_path, scenario_path, f"invalid {file_format.upper()}"
    if category == "schema":
        document["metrics"] = []
        expected = "metrics"
    elif file_format == "json":
        scenario["rules"][0]["oid"] = "1.3.6.1.4.1.55555.999.0"
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        expected = "unknown scenario OID"
    else:
        document["scenarios"][0]["actions"][0]["metric"] = "missing_metric"
        expected = "unknown scenario metric"

    content = json.dumps(document) if file_format == "json" else yaml.safe_dump(document)
    device_path.write_text(content, encoding="utf-8")
    return device_path, scenario_path, expected


@pytest.mark.parametrize("file_format", ["json", "yaml"])
@pytest.mark.parametrize("category", ["read", "encoding", "parse", "schema", "links"])
def test_cli_configuration_errors_have_timestamp_without_traceback(tmp_path, file_format, category):
    device, scenario, expected = invalid_configuration(tmp_path, file_format, category)
    arguments = (
        ["--device-config", str(device), "--scenario", str(scenario)]
        if file_format == "json"
        else ["--config", str(device)]
    )

    result = run_python("-m", "snmp_emulator", *arguments, "--check-config", "--log-level", "ERROR")

    assert result.returncode == 2
    assert result.stdout == ""
    assert re.match(rf"{TIMESTAMP} ERROR \S+: configuration error: ", result.stderr)
    assert result.stderr.count("configuration error:") == 1
    assert expected in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_cli_applies_selected_level_before_loading_configuration(level):
    result = run_python(
        "-c",
        """
import logging
import sys
from snmp_emulator import __main__ as cli

original = cli.load_config
def load(path):
    logging.getLogger('snmp_emulator.loader').info('loading configuration')
    logging.getLogger('snmp_emulator.loader').debug('configuration details')
    return original(path)

cli.load_config = load
raise SystemExit(cli.main([
    '--config', 'examples/device.yaml', '--check-config', '--log-level', sys.argv[1],
]))
""",
        level,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "configuration is valid: 2 metrics\n"
    assert ("loading configuration" in result.stderr) == (level in ("DEBUG", "INFO"))
    assert ("configuration details" in result.stderr) == (level == "DEBUG")


@pytest.mark.parametrize("category", ["read", "encoding", "parse", "schema", "links"])
def test_gui_configuration_errors_are_logged_once_and_shown(tmp_path, category, caplog):
    device, scenario, expected = invalid_configuration(tmp_path, "json", category)
    controller = PanelController()

    with caplog.at_level(logging.ERROR):
        controller._run(device, scenario)

    records = [record for record in caplog.records if record.name == gui_controller.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage().startswith("configuration error: ")
    assert expected in records[0].getMessage()
    assert records[0].exc_info is None
    assert controller.snapshot().state == "error"
    assert expected in controller.snapshot().error


def test_busy_port_is_logged_once_by_cli_and_gui(tmp_path, caplog):
    document = json.loads((EXAMPLES / "device.json").read_text(encoding="utf-8"))
    device = tmp_path / "device.json"
    scenario = EXAMPLES / "stable.json"
    controller = PanelController()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        document["agent"].update(host="127.0.0.1", port=port)
        device.write_text(json.dumps(document), encoding="utf-8")
        result = run_python(
            "-m", "snmp_emulator", "--device-config", str(device), "--scenario", str(scenario)
        )
        with caplog.at_level(logging.ERROR):
            controller._run(device, scenario)

    assert result.returncode == 1
    assert re.match(rf"{TIMESTAMP} ERROR \S+: emulator failed: cannot bind ", result.stderr)
    assert f"udp://127.0.0.1:{port}" in result.stderr
    assert result.stderr.count("emulator failed:") == 1
    assert "Traceback" not in result.stderr
    records = [record for record in caplog.records if record.name == gui_controller.__name__]
    assert len(records) == 1
    assert records[0].getMessage().startswith("emulator failed: cannot bind ")
    assert records[0].exc_info is None
    assert controller.snapshot().state == "error"
    assert f"udp://127.0.0.1:{port}" in controller.snapshot().error


def test_gui_unexpected_error_is_logged_once_with_details(monkeypatch, caplog):
    async def fail(_self):
        raise RuntimeError("scenario failure details")

    monkeypatch.setattr(gui_controller.EmulatorApplication, "run", fail)
    controller = PanelController()
    with caplog.at_level(logging.ERROR):
        controller._run(EXAMPLES / "device.json", EXAMPLES / "stable.json")

    records = [record for record in caplog.records if record.name == gui_controller.__name__]
    assert len(records) == 1
    assert records[0].getMessage() == "emulator stopped unexpectedly"
    assert records[0].exc_info is not None
    assert "scenario failure details" in caplog.text
    assert "scenario failure details" in controller.snapshot().error


def test_gui_configures_logging_before_opening_window(monkeypatch, caplog):
    pytest.importorskip("tkinter")
    from snmp_emulator import gui

    configured = []
    monkeypatch.setattr(gui, "configure_logging", lambda: configured.append(True))

    def cannot_open():
        assert configured == [True]
        raise gui.tk.TclError("display unavailable")

    monkeypatch.setattr(gui.tk, "Tk", cannot_open)
    with caplog.at_level(logging.ERROR):
        assert gui.main() == 1

    records = [record for record in caplog.records if record.name == gui.__name__]
    assert len(records) == 1
    assert "display unavailable" in records[0].getMessage()
    assert records[0].exc_info is None
