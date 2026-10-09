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
    assert "configuration error: cannot read" in capsys.readouterr().out


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
