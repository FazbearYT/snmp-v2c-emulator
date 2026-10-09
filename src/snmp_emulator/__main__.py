from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .application import EmulatorApplication
from .config import ConfigurationError, load_config


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config is None:
        build_parser().error("--config is required unless --version is used")
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}")
        return 2
    if args.check_config:
        print(f"configuration is valid: {len(config.metrics)} metrics")
        return 0
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(EmulatorApplication(config).run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("emulator stopped")
    except OSError as exc:
        logging.getLogger(__name__).error("emulator failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
