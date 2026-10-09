from importlib.metadata import version

from snmp_emulator import __version__


def test_package_version_matches_runtime_version() -> None:
    assert version("snmp-v2c-emulator") == __version__
