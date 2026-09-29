from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import ConfigurationError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SNMP v2c device emulator")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="path to a YAML configuration")
    parser.add_argument("--check-config", action="store_true")
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
    # TODO: pass the validated configuration to the runtime service.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
