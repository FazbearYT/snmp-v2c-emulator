from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = Field(default=1161, ge=1, le=65535)
    community: str = Field(default="public", min_length=1)


class MetricConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    oid: str
    type: str
    initial: Any
    access: Literal["read-only"] = "read-only"

    @field_validator("oid")
    @classmethod
    def validate_oid(cls, value: str) -> str:
        parts = value.strip(".").split(".")
        try:
            numbers = [int(part) for part in parts]
        except ValueError as exc:
            raise ValueError("OID components must be integers") from exc
        if len(numbers) < 2 or numbers[0] not in (0, 1, 2):
            raise ValueError("invalid OID")
        if numbers[0] < 2 and numbers[1] > 39:
            raise ValueError("invalid second OID component")
        if any(number < 0 for number in numbers):
            raise ValueError("OID components cannot be negative")
        return ".".join(str(number) for number in numbers)


class EmulatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    agent: AgentConfig = Field(default_factory=AgentConfig)
    metrics: list[MetricConfig] = Field(min_length=1)

    @field_validator("metrics")
    @classmethod
    def validate_unique_metrics(cls, metrics: list[MetricConfig]) -> list[MetricConfig]:
        names = [metric.name for metric in metrics]
        oids = [metric.oid for metric in metrics]
        if len(names) != len(set(names)):
            raise ValueError("metric names must be unique")
        if len(oids) != len(set(oids)):
            raise ValueError("metric OIDs must be unique")
        return metrics


class ConfigurationError(ValueError):
    pass


def load_config(path: str | Path) -> EmulatorConfig:
    source = Path(path)
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
        return EmulatorConfig.model_validate(document)
    except OSError as exc:
        raise ConfigurationError(f"cannot read {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML: {exc}") from exc
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc
