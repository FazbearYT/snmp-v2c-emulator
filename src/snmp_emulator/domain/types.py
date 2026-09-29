from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Any


class SnmpDataType(StrEnum):
    INTEGER = "Integer"
    OCTET_STRING = "OctetString"
    OBJECT_IDENTIFIER = "ObjectIdentifier"
    IP_ADDRESS = "IpAddress"
    COUNTER32 = "Counter32"
    GAUGE32 = "Gauge32"
    TIME_TICKS = "TimeTicks"
    COUNTER64 = "Counter64"


def validate_snmp_value(data_type: SnmpDataType, value: Any) -> Any:
    if data_type is SnmpDataType.OCTET_STRING:
        if not isinstance(value, str):
            raise ValueError("OctetString value must be a string")
        return value
    if data_type is SnmpDataType.OBJECT_IDENTIFIER:
        from .oid import ObjectIdentifier

        return str(ObjectIdentifier.parse(str(value)))
    if data_type is SnmpDataType.IP_ADDRESS:
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
