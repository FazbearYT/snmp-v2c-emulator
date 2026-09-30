from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .oid import ObjectIdentifier
from .types import SnmpDataType, validate_snmp_value


@dataclass(slots=True)
class Metric:
    name: str
    oid: ObjectIdentifier
    data_type: SnmpDataType
    value: Any = field(repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("metric name cannot be empty")
        self.value = validate_snmp_value(self.data_type, self.value)

    def update(self, value: Any) -> None:
        self.value = validate_snmp_value(self.data_type, value)
