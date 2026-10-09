from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .application import EmulatorApplication
from .config import ConfigurationError, load_config

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SNMP v2c device emulator")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="path to a YAML configuration")
    parser.add_argument("--check-config", action="store_true")
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
    args = build_parser().parse_args(argv)
    if args.config is None:
        build_parser().error("--config is required unless --version is used")
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        print(f"configuration is valid: {len(config.metrics)} metrics")
        return 0
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        stopped = asyncio.run(run_application(EmulatorApplication(config)))
        if stopped:
            LOGGER.info("emulator stopped")
    except KeyboardInterrupt:
        LOGGER.info("emulator stopped")
    except OSError as exc:
        LOGGER.error("emulator failed: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("emulator stopped unexpectedly")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
