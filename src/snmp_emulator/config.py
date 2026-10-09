from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .domain.types import SnmpDataType, validate_snmp_value


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = Field(default=1161, ge=1, le=65535)
    community: str = Field(default="public", min_length=1)


class MetricConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    oid: str
    type: SnmpDataType
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

    @field_validator("initial")
    @classmethod
    def validate_initial(cls, value: Any, info: Any) -> Any:
        data_type = info.data.get("type")
        if data_type is None:
            return value
        return validate_snmp_value(data_type, value)


class SetActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["set"]
    metric: str
    at: float = Field(ge=0)
    value: Any


class SequenceActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["sequence"]
    metric: str
    at: float = Field(default=0, ge=0)
    interval: float = Field(gt=0)
    values: list[Any] = Field(min_length=1)


class RampActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ramp"]
    metric: str
    at: float = Field(default=0, ge=0)
    duration: float = Field(gt=0)
    start: int
    end: int


class StepActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["step"]
    metric: str
    at: float = Field(default=0, ge=0)
    interval: float = Field(gt=0)
    start: int
    amount: int
    minimum: int | None = None
    maximum: int | None = None

    @model_validator(mode="after")
    def validate_limits(self) -> StepActionConfig:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("step minimum cannot exceed maximum")
        return self


ActionConfig = Annotated[
    SetActionConfig | SequenceActionConfig | RampActionConfig | StepActionConfig,
    Field(discriminator="type"),
]


class ScenarioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    repeat_every: float | None = Field(default=None, gt=0)
    actions: list[ActionConfig] = Field(min_length=1)


class EmulatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    agent: AgentConfig = Field(default_factory=AgentConfig)
    metrics: list[MetricConfig] = Field(min_length=1)
    scenarios: list[ScenarioConfig] = Field(default_factory=list)

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

    @model_validator(mode="after")
    def validate_scenarios(self) -> EmulatorConfig:
        metrics = {metric.name: metric for metric in self.metrics}
        scenario_names = [scenario.name for scenario in self.scenarios]
        if len(scenario_names) != len(set(scenario_names)):
            raise ValueError("scenario names must be unique")
        for scenario in self.scenarios:
            for action in scenario.actions:
                if scenario.repeat_every is not None and action.at >= scenario.repeat_every:
                    raise ValueError(
                        f"scenario action at={action.at} must be less than "
                        f"repeat_every={scenario.repeat_every}"
                    )
                metric = metrics.get(action.metric)
                if metric is None:
                    raise ValueError(f"unknown scenario metric: {action.metric}")
                if isinstance(action, SetActionConfig):
                    validate_snmp_value(metric.type, action.value)
                elif isinstance(action, SequenceActionConfig):
                    for value in action.values:
                        validate_snmp_value(metric.type, value)
                else:
                    if metric.type in {
                        SnmpDataType.OCTET_STRING,
                        SnmpDataType.OBJECT_IDENTIFIER,
                        SnmpDataType.IP_ADDRESS,
                    }:
                        raise ValueError(f"{action.type} requires a numeric metric")
                    if isinstance(action, RampActionConfig):
                        validate_snmp_value(metric.type, action.start)
                        validate_snmp_value(metric.type, action.end)
                    elif isinstance(action, StepActionConfig):
                        self._validate_step_action(scenario, action, metric.type)
        return self

    @staticmethod
    def _validate_step_action(
        scenario: ScenarioConfig,
        action: StepActionConfig,
        data_type: SnmpDataType,
    ) -> None:
        for value in (action.start, action.minimum, action.maximum):
            if value is not None:
                validate_snmp_value(data_type, value)

        if scenario.repeat_every is None:
            if action.amount > 0 and action.maximum is None:
                raise ValueError(
                    "a non-repeating positive step requires maximum to prevent overflow"
                )
            if action.amount < 0 and action.minimum is None:
                raise ValueError(
                    "a non-repeating negative step requires minimum to prevent overflow"
                )
            return

        active_duration = scenario.repeat_every - action.at
        last_step = max(0, math.ceil(active_duration / action.interval) - 1)
        final_value = action.start + last_step * action.amount
        if action.minimum is not None:
            final_value = max(final_value, action.minimum)
        if action.maximum is not None:
            final_value = min(final_value, action.maximum)
        validate_snmp_value(data_type, final_value)


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
