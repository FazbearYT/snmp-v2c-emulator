from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True, slots=True)
class ObjectIdentifier:
    parts: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.parts) < 2:
            raise ValueError("OID must have at least two components")
        if self.parts[0] not in (0, 1, 2):
            raise ValueError("first OID component must be 0, 1 or 2")
        if self.parts[0] < 2 and self.parts[1] > 39:
            raise ValueError("second OID component must be at most 39")
        if any(part < 0 for part in self.parts):
            raise ValueError("OID components cannot be negative")

    @classmethod
    def parse(cls, value: str) -> ObjectIdentifier:
        try:
            parts = tuple(int(part) for part in value.strip(".").split("."))
        except ValueError as exc:
            raise ValueError("OID components must be integers") from exc
        return cls(parts)

    def __str__(self) -> str:
        return ".".join(str(part) for part in self.parts)
