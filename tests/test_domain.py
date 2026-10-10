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


@pytest.mark.parametrize(
    "value, message",
    [
        ("1.3." + str(2**32), "cannot exceed"),
        (".".join(["1", "3", *(["1"] * 127)]), "128 components"),
    ],
)
def test_rejects_oid_outside_smiv2_limits(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ObjectIdentifier.parse(value)


def test_validates_unsigned_value_range() -> None:
    assert validate_snmp_value(SnmpDataType.GAUGE32, 42) == 42
    with pytest.raises(ValueError, match="unsigned"):
        validate_snmp_value(SnmpDataType.GAUGE32, -1)


def test_normalizes_ip_address() -> None:
    assert validate_snmp_value(SnmpDataType.IP_ADDRESS, "192.0.2.1") == "192.0.2.1"


@pytest.mark.parametrize(
    "data_type, value",
    [
        (SnmpDataType.IP_ADDRESS, True),
        (SnmpDataType.IP_ADDRESS, 1),
        (SnmpDataType.OBJECT_IDENTIFIER, 1.3),
    ],
)
def test_rejects_non_string_textual_values(data_type: SnmpDataType, value: object) -> None:
    with pytest.raises(ValueError, match="must be a string"):
        validate_snmp_value(data_type, value)


def test_validates_octet_string_utf8_size() -> None:
    assert validate_snmp_value(SnmpDataType.OCTET_STRING, "привет") == "привет"
    with pytest.raises(ValueError, match="65535 UTF-8 bytes"):
        validate_snmp_value(SnmpDataType.OCTET_STRING, "я" * 32768)
