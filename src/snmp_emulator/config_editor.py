from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from shutil import copy2, copymode
from tempfile import TemporaryDirectory
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from .config import ConfigurationError, EmulatorConfig
from .json_config import (
    DeviceConfig,
    ScenarioDocument,
    SequenceRule,
    StepRule,
    bind_config,
    read_json,
)

DocumentName = Literal["device", "scenario"]
Model = TypeVar("Model", bound=BaseModel)

ROOT_FIELDS = {
    "metric names must be unique": "metrics",
    "metric OIDs must be unique": "metrics",
    "scenario requires rules, unavailability or OID responses": "rules",
    "scenario may contain only one rule per OID": "rules",
    "jump at must not exceed duration": "rules",
    "OID response configuration may contain only one mode per OID": "oid_responses",
    "unavailability interval must end within scenario duration": "unavailability",
    "unavailability requires constant metric values": "unavailability",
}


@dataclass(frozen=True)
class EditorIssue:
    document: DocumentName
    field: tuple[str | int, ...]
    message: str


class EditorValidationError(ConfigurationError):
    def __init__(self, issues: tuple[EditorIssue, ...]) -> None:
        self.issues = issues
        lines = []
        for issue in issues:
            name = "Устройство" if issue.document == "device" else "Сценарий"
            field = ".".join(str(part) for part in issue.field)
            location = f"{name}, {field}" if field else name
            lines.append(f"{location}: {issue.message}")
        super().__init__("\n".join(lines))


def _validate_document(model: type[Model], data: Any, document: DocumentName) -> Model:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        issues = []
        for error in exc.errors(include_url=False, include_input=False):
            field = error["loc"]
            message = error["msg"].removeprefix("Value error, ")
            if not field and message in ROOT_FIELDS:
                field = (ROOT_FIELDS[message],)
            if field == ("agent",) and error["type"] == "value_error":
                field = ("agent", "host")
            if field[:1] == ("rules",) and len(field) > 2:
                field = (*field[:2], *field[3:])
            if field[:1] == ("rules",) and error["type"].startswith("union_tag_"):
                field = (*field, "type")
            if field == ("rules",) and message == "jump at must not exceed duration":
                duration = float(data["duration"])
                for index, rule in enumerate(data["rules"]):
                    if rule["type"] == "jump" and float(rule["at"]) > duration:
                        field = ("rules", index, "at")
                        break
            if error["type"] == "extra_forbidden":
                message = "Поле не поддерживается редактором."
            if error["type"] == "union_tag_invalid":
                message = "Тип правила не поддерживается редактором."
            issues.append(EditorIssue(document, field, message))
        raise EditorValidationError(tuple(issues)) from exc


def read_device(path: str | Path) -> dict[str, Any]:
    data = read_json(path)
    _validate_document(DeviceConfig, data, "device")
    return data


def read_scenario(path: str | Path) -> dict[str, Any]:
    data = read_json(path)
    _validate_document(ScenarioDocument, data, "scenario")
    return data


def _binding_message(error: ValueError) -> str:
    if isinstance(error, ValidationError):
        return error.errors(include_input=False, include_url=False)[0]["msg"].removeprefix(
            "Value error, "
        )
    return str(error)


def _binding_issues(
    device: DeviceConfig, scenario: ScenarioDocument, error: ValueError
) -> tuple[EditorIssue, ...]:
    issues = []
    for index, rule in enumerate(scenario.rules):
        sample = scenario.model_copy(update={"rules": [rule], "oid_responses": []})
        try:
            bind_config(device, sample)
        except ValueError as exc:
            message = _binding_message(exc)
            if (
                message.startswith("unknown scenario OID")
                or message == "step requires a numeric metric"
            ):
                field = "oid"
            elif isinstance(rule, StepRule):
                field = (
                    "step" if message == "step must move from initial towards limit" else "limit"
                )
            elif isinstance(rule, SequenceRule):
                field = "values"
            else:
                field = "value"
            issues.append(EditorIssue("scenario", ("rules", index, field), message))

    for index, response in enumerate(scenario.oid_responses):
        sample = scenario.model_copy(update={"rules": [], "oid_responses": [response]})
        try:
            bind_config(device, sample)
        except ValueError as exc:
            issues.append(
                EditorIssue("scenario", ("oid_responses", index, "oid"), _binding_message(exc))
            )

    return tuple(issues) or (EditorIssue("scenario", (), _binding_message(error)),)


def validate_pair(device: dict[str, Any], scenario: dict[str, Any]) -> EmulatorConfig:
    device_model = _validate_document(DeviceConfig, device, "device")
    scenario_model = _validate_document(ScenarioDocument, scenario, "scenario")
    try:
        return bind_config(device_model, scenario_model)
    except ValueError as exc:
        raise EditorValidationError(_binding_issues(device_model, scenario_model, exc)) from exc


def _check_distinct_paths(device: Path, scenario: Path) -> None:
    if device == scenario or (device.exists() and scenario.exists() and device.samefile(scenario)):
        raise ConfigurationError("Для устройства и сценария нужны разные файлы.")


def _write_pair(device: Path, scenario: Path, device_text: str, scenario_text: str) -> None:
    with (
        TemporaryDirectory(dir=device.parent) as device_folder,
        TemporaryDirectory(dir=scenario.parent) as scenario_folder,
    ):
        device_temp = Path(device_folder) / "device.json"
        scenario_temp = Path(scenario_folder) / "scenario.json"
        backup = Path(device_folder) / "device-old.json"
        device_temp.write_text(device_text, encoding="utf-8")
        scenario_temp.write_text(scenario_text, encoding="utf-8")
        if device.exists():
            copy2(device, backup)
            copymode(device, device_temp)
        if scenario.exists():
            copymode(scenario, scenario_temp)

        device_temp.replace(device)
        try:
            _check_distinct_paths(device, scenario)
            scenario_temp.replace(scenario)
        except (OSError, ConfigurationError):
            if backup.exists():
                backup.replace(device)
            else:
                device.unlink()
            raise


def save_pair(
    device_path: str | Path,
    scenario_path: str | Path,
    device: dict[str, Any],
    scenario: dict[str, Any],
) -> None:
    validate_pair(device, scenario)
    try:
        device_file = Path(device_path).resolve()
        scenario_file = Path(scenario_path).resolve()
        _check_distinct_paths(device_file, scenario_file)
        for path in (device_file, scenario_file):
            if path.exists() and not path.is_file():
                raise ConfigurationError(f"Выберите путь к файлу: {path}")
        device_text = json.dumps(device, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        scenario_text = json.dumps(scenario, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        _write_pair(device_file, scenario_file, device_text, scenario_text)
    except ConfigurationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError(f"Не удалось сохранить конфигурацию: {exc}") from exc
