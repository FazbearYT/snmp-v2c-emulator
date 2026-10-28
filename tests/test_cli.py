import asyncio
import logging
import signal
from pathlib import Path

import pytest

from snmp_emulator import __main__ as cli

EXAMPLE_CONFIG = Path(__file__).parents[1] / "examples" / "device.yaml"
JSON_DEVICE = EXAMPLE_CONFIG.with_suffix(".json")
JSON_SCENARIO = EXAMPLE_CONFIG.parent / "cpu-step.json"


@pytest.mark.parametrize("scenario", ["stable", "temperature-jump", "cpu-step"])
def test_checks_json_pair_without_creating_application(monkeypatch, capsys, scenario) -> None:
    def unexpected_application(config):
        pytest.fail("--check-config must not create application or open a port")

    monkeypatch.setattr(cli, "EmulatorApplication", unexpected_application)
    assert (
        cli.main(
            [
                "--device-config",
                str(JSON_DEVICE),
                "--scenario",
                str(JSON_DEVICE.parent / f"{scenario}.json"),
                "--check-config",
            ]
        )
        == 0
    )
    assert "5 metrics" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--device-config", str(JSON_DEVICE)],
        ["--scenario", str(JSON_SCENARIO)],
        [
            "--config",
            str(EXAMPLE_CONFIG),
            "--device-config",
            str(JSON_DEVICE),
            "--scenario",
            str(JSON_SCENARIO),
        ],
    ],
)
def test_requires_unambiguous_configuration_arguments(args, capsys) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_invalid_json_pair_is_rejected_before_application(monkeypatch, tmp_path, capsys) -> None:
    def unexpected_application(config):
        pytest.fail("invalid pair must not create application")

    monkeypatch.setattr(cli, "EmulatorApplication", unexpected_application)
    path = tmp_path / "bad.json"
    path.write_text('{"duration":1,"update_interval":1,"rules":[]}', encoding="utf-8")
    assert cli.main(["--device-config", str(JSON_DEVICE), "--scenario", str(path)]) == 2
    captured = capsys.readouterr()
    assert "configuration error:" in captured.err
    assert "Traceback" not in captured.err


def test_runs_json_application(monkeypatch) -> None:
    received = []

    class Application:
        def __init__(self, config):
            received.append(config)

        async def run(self):
            pass

    monkeypatch.setattr(cli, "EmulatorApplication", Application)
    assert (
        cli.main(
            [
                "--device-config",
                str(JSON_DEVICE),
                "--scenario",
                str(JSON_SCENARIO),
                "--log-level",
                "DEBUG",
            ]
        )
        == 0
    )
    assert received[0].duration == 20


def test_version_needs_no_configuration(capsys) -> None:
    with pytest.raises(SystemExit) as result:
        cli.main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == cli.__version__


def test_checks_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    result = cli.main(["--config", str(EXAMPLE_CONFIG), "--check-config"])

    assert result == 0
    assert capsys.readouterr().out == "configuration is valid: 2 metrics\n"


def test_reports_configuration_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = cli.main(["--config", str(tmp_path / "missing.yaml"), "--check-config"])

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration error: cannot read" in captured.err


def test_reports_non_utf8_configuration_as_cli_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "invalid-encoding.yaml"
    config_path.write_bytes(b"\xff")

    result = cli.main(["--config", str(config_path), "--check-config"])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "cannot decode" in captured.err


def test_runs_application(monkeypatch: pytest.MonkeyPatch) -> None:
    was_run = False

    class FakeApplication:
        def __init__(self, config: object) -> None:
            assert config.metrics

        async def run(self) -> None:
            nonlocal was_run
            was_run = True

    monkeypatch.setattr(cli, "EmulatorApplication", FakeApplication)

    assert cli.main(["--config", str(EXAMPLE_CONFIG)]) == 0
    assert was_run


def test_requires_configuration_path() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([])

    assert error.value.code == 2


def test_reports_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingApplication:
        def __init__(self, config: object) -> None:
            pass

        async def run(self) -> None:
            raise OSError("cannot bind udp port")

    monkeypatch.setattr(cli, "EmulatorApplication", FailingApplication)

    assert cli.main(["--config", str(EXAMPLE_CONFIG)]) == 1


def test_reports_unexpected_application_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingApplication:
        def __init__(self, config: object) -> None:
            pass

        async def run(self) -> None:
            raise RuntimeError("scenario failed")

    monkeypatch.setattr(cli, "EmulatorApplication", FailingApplication)

    assert cli.main(["--config", str(EXAMPLE_CONFIG)]) == 1


def test_reports_nested_application_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingApplication:
        def __init__(self, config: object) -> None:
            pass

        async def run(self) -> None:
            raise ExceptionGroup("scenario task failed", [RuntimeError("scenario failed")])

    monkeypatch.setattr(cli, "EmulatorApplication", FailingApplication)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["--config", str(EXAMPLE_CONFIG)]) == 1
    assert "scenario failed" in caplog.text


@pytest.mark.asyncio
async def test_sigterm_requests_graceful_application_stop() -> None:
    class SignalLoop:
        def __init__(self) -> None:
            self.callback: object | None = None
            self.removed: object | None = None

        def add_signal_handler(self, selected: object, callback: object) -> None:
            assert selected == signal.SIGTERM
            self.callback = callback

        def remove_signal_handler(self, selected: object) -> bool:
            self.removed = selected
            return True

    class WaitingApplication:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.stopped = False

        async def run(self) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped = True

    loop = SignalLoop()
    application = WaitingApplication()
    task = asyncio.create_task(cli.run_application(application, loop=loop))
    await application.started.wait()
    assert callable(loop.callback)

    loop.callback()

    assert await task is True
    assert application.stopped
    assert loop.removed == signal.SIGTERM
