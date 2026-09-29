import pytest

from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.types import SnmpDataType, validate_snmp_value


def test_oid_comparison_is_numeric() -> None:
    left = ObjectIdentifier.parse("1.3.6.1.2.9")
    right = ObjectIdentifier.parse("1.3.6.1.2.10")

    assert left < right
    assert str(right) == "1.3.6.1.2.10"


def test_rejects_invalid_oid_root() -> None:
    with pytest.raises(ValueError, match="first"):
        ObjectIdentifier.parse("3.1.1")


def test_validates_unsigned_value_range() -> None:
    assert validate_snmp_value(SnmpDataType.GAUGE32, 42) == 42
    with pytest.raises(ValueError, match="unsigned"):
        validate_snmp_value(SnmpDataType.GAUGE32, -1)


def test_normalizes_ip_address() -> None:
    assert validate_snmp_value(SnmpDataType.IP_ADDRESS, "192.0.2.1") == "192.0.2.1"
