from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .application import EmulatorApplication
from .config import ConfigurationError, load_config
from .json_config import load_json_config
from .logging_config import configure_logging
from .run_report import default_report_path

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SNMP v2c device emulator")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="path to a YAML configuration")
    parser.add_argument("--device-config", type=Path, help="path to a JSON device configuration")
    parser.add_argument("--scenario", type=Path, help="path to a JSON scenario")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--report-file", type=Path, help="JSON report destination")

    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


async def run_application(
    application: Any,
    loop: asyncio.AbstractEventLoop | None = None,
) -> bool:
    active_loop = loop or asyncio.get_running_loop()
    current_task = asyncio.current_task()
    stop_requested = False

    def request_stop() -> None:
        nonlocal stop_requested
        stop_requested = True
        if current_task is not None:
            current_task.cancel()

    signal_handler_installed = False
    if hasattr(signal, "SIGTERM"):
        try:
            active_loop.add_signal_handler(signal.SIGTERM, request_stop)
            signal_handler_installed = True
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await application.run()
    except asyncio.CancelledError:
        if not stop_requested:
            raise
        return True
    finally:
        if signal_handler_installed:
            active_loop.remove_signal_handler(signal.SIGTERM)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    if args.config is not None:
        if args.device_config is not None or args.scenario is not None:
            parser.error("--config cannot be combined with --device-config or --scenario")
    elif args.device_config is None or args.scenario is None:
        parser.error("both --device-config and --scenario are required (or use --config for YAML)")
    try:
        config = (
            load_config(args.config)
            if args.config is not None
            else load_json_config(args.device_config, args.scenario)
        )
    except ConfigurationError as exc:
        LOGGER.error("configuration error: %s", exc)
        return 2
    if args.check_config:
        print(f"configuration is valid: {len(config.metrics)} metrics")
        return 0
    try:
        source = args.scenario if args.scenario is not None else args.config
        path = args.report_file or default_report_path(source)
        destination = path.resolve()
        for input_path in (args.config, args.device_config, args.scenario):
            if input_path is not None and (
                destination == input_path.resolve()
                or (destination.exists() and destination.samefile(input_path))
            ):
                LOGGER.error("report destination must differ from input configuration: %s", path)
                return 2
        application = EmulatorApplication(config)
        if hasattr(application, "configure_report"):
            application.configure_report(scenario_id=source.stem, path=path)
        asyncio.run(run_application(application))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        LOGGER.error("emulator failed: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("emulator stopped unexpectedly")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
