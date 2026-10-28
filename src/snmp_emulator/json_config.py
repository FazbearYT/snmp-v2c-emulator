from __future__ import annotations

import json
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import AgentConfig, ConfigurationError, EmulatorConfig, MetricConfig, ScenarioConfig
from .domain.oid import ObjectIdentifier

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)]


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: AgentConfig
    metrics: list[MetricConfig] = Field(min_length=1)

    @field_validator("agent")
    @classmethod
    def validate_agent(cls, value: AgentConfig) -> AgentConfig:
        IPv4Address(value.host)
        return value

    @model_validator(mode="after")
    def validate_metrics(self) -> DeviceConfig:
        EmulatorConfig.validate_unique_metrics(self.metrics)
        return self


class OidRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oid: str

    @field_validator("oid")
    @classmethod
    def normalize_oid(cls, value: str) -> str:
        return str(ObjectIdentifier.parse(value))


class ConstantRule(OidRule):
    type: Literal["constant"]
    value: Any


class JumpRule(OidRule):
    type: Literal["jump"]
    at: Seconds
    value: Any


class StepRule(OidRule):
    type: Literal["step"]
    step: int = Field(strict=True)
    limit: int = Field(strict=True)

    @field_validator("step")
    @classmethod
    def validate_step(cls, value: int) -> int:
        if value == 0:
            raise ValueError("step must be nonzero")
        return value


Rule = Annotated[ConstantRule | JumpRule | StepRule, Field(discriminator="type")]


class ScenarioDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration: PositiveSeconds
    update_interval: PositiveSeconds
    rules: list[Rule] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rules(self) -> ScenarioDocument:
        oids = [rule.oid for rule in self.rules]
        if len(set(oids)) != len(oids):
            raise ValueError("scenario may contain only one rule per OID")
        for rule in self.rules:
            if isinstance(rule, JumpRule) and rule.at > self.duration:
                raise ValueError("jump at must not exceed duration")
        return self


def bind_config(device: DeviceConfig, scenario: ScenarioDocument) -> EmulatorConfig:
    metrics = {metric.oid: metric for metric in device.metrics}
    actions: list[dict[str, Any]] = []
    for rule in scenario.rules:
        metric = metrics.get(rule.oid)
        if metric is None:
            raise ValueError(f"unknown scenario OID: {rule.oid}")
        if isinstance(rule, ConstantRule | JumpRule):
            actions.append(
                {
                    "type": "set",
                    "metric": metric.name,
                    "at": rule.at if isinstance(rule, JumpRule) else 0.0,
                    "value": rule.value,
                }
            )
        else:
            if type(metric.initial) is not int:
                raise ValueError("step requires a numeric metric")
            if (rule.limit - metric.initial) * rule.step < 0:
                raise ValueError("step must move from initial towards limit")
            actions.append(
                {
                    "type": "step",
                    "metric": metric.name,
                    "at": 0.0,
                    "interval": scenario.update_interval,
                    "start": metric.initial,
                    "amount": rule.step,
                    "maximum" if rule.step > 0 else "minimum": rule.limit,
                }
            )
    return EmulatorConfig(
        schema_version=1,
        agent=device.agent,
        metrics=device.metrics,
        scenarios=[ScenarioConfig(name="json", actions=actions)],
        duration=scenario.duration,
        update_interval=scenario.update_interval,
    )


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def read_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        return json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=unique_json_object,
            parse_constant=reject_json_constant,
        )
    except OSError as exc:
        raise ConfigurationError(f"cannot read {source}: {exc}") from exc
    except UnicodeError as exc:
        raise ConfigurationError(f"cannot decode {source} as UTF-8: {exc}") from exc
    except (ValueError, RecursionError) as exc:
        raise ConfigurationError(f"invalid JSON in {source}: {exc}") from exc


def load_json_config(device_path: str | Path, scenario_path: str | Path) -> EmulatorConfig:
    try:
        device = DeviceConfig.model_validate(read_json(device_path))
        scenario = ScenarioDocument.model_validate(read_json(scenario_path))
        return bind_config(device, scenario)
    except (ValidationError, ValueError) as exc:
        raise ConfigurationError(str(exc)) from exc
