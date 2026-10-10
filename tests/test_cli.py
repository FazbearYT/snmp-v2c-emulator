import asyncio
import logging
import signal
from pathlib import Path

import pytest

from snmp_emulator import __main__ as cli

EXAMPLE_CONFIG = Path(__file__).parents[1] / "examples" / "device.yaml"


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
