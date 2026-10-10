from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Any

MAX_OCTET_STRING_BYTES = 65535


class SnmpDataType(StrEnum):
    INTEGER = "Integer"
    OCTET_STRING = "OctetString"
    OBJECT_IDENTIFIER = "ObjectIdentifier"
    IP_ADDRESS = "IpAddress"
    COUNTER32 = "Counter32"
    GAUGE32 = "Gauge32"
    TIME_TICKS = "TimeTicks"
    COUNTER64 = "Counter64"


def encode_octet_string(value: str) -> bytes:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("OctetString value must be valid Unicode text") from exc
    if len(encoded) > MAX_OCTET_STRING_BYTES:
        raise ValueError(f"OctetString value cannot exceed {MAX_OCTET_STRING_BYTES} UTF-8 bytes")
    return encoded


def validate_snmp_value(data_type: SnmpDataType, value: Any) -> Any:
    if data_type is SnmpDataType.OCTET_STRING:
        if not isinstance(value, str):
            raise ValueError("OctetString value must be a string")
        encode_octet_string(value)
        return value
    if data_type is SnmpDataType.OBJECT_IDENTIFIER:
        from .oid import ObjectIdentifier

        if not isinstance(value, str):
            raise ValueError("ObjectIdentifier value must be a string")
        return str(ObjectIdentifier.parse(value))
    if data_type is SnmpDataType.IP_ADDRESS:
        if not isinstance(value, str):
            raise ValueError("IpAddress value must be a string")
        return str(ipaddress.IPv4Address(value))
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{data_type.value} value must be an integer")
    if data_type is SnmpDataType.INTEGER:
        if not -(2**31) <= value < 2**31:
            raise ValueError("Integer value is outside the signed 32-bit range")
        return value
    upper = 2**64 - 1 if data_type is SnmpDataType.COUNTER64 else 2**32 - 1
    if not 0 <= value <= upper:
        raise ValueError(f"{data_type.value} value is outside its unsigned range")
    return value
