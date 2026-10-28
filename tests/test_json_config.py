import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from snmp_emulator.config import ConfigurationError
from snmp_emulator.domain.types import SnmpDataType
from snmp_emulator.json_config import (
    DeviceConfig,
    ScenarioDocument,
    bind_config,
    load_json_config,
    read_json,
)

EXAMPLES = Path(__file__).parents[1] / "examples"
OID = "1.3.6.1.4.1.55555.1.1.0"


def device_document() -> dict:
    return {
        "agent": {},
        "metrics": [{"name": "cpu", "oid": OID, "type": "Integer32", "initial": 10}],
    }


def scenario_document(**rule_fields: object) -> dict:
    return {
        "duration": 1,
        "update_interval": 0.1,
        "rules": [{"type": "step", "oid": OID, "step": 3, "limit": 20, **rule_fields}],
    }


@pytest.mark.parametrize("name", ["stable", "temperature-jump", "cpu-step"])
def test_loads_example_pairs(name: str) -> None:
    config = load_json_config(EXAMPLES / "device.json", EXAMPLES / f"{name}.json")
    assert len(config.metrics) == 5
    assert config.metrics[0].type is SnmpDataType.INTEGER32
    assert config.duration > 0
    assert config.update_interval > 0


def test_normalizes_rule_oid_and_maps_to_metric_name() -> None:
    device = DeviceConfig.model_validate(device_document())
    scenario = ScenarioDocument.model_validate(scenario_document(oid=".01.3.6.1.4.1.55555.1.1.0"))
    config = bind_config(device, scenario)
    action = config.scenarios[0].actions[0]
    assert action.metric == "cpu"
    assert action.maximum == 20
    assert action.interval == 0.1


@pytest.mark.parametrize("initial", [-(2**31), 2**31 - 1])
@pytest.mark.parametrize("name", ["Integer32", "Integer"])
def test_accepts_signed_integer_boundaries_and_alias(initial: int, name: str) -> None:
    document = device_document()
    document["metrics"][0].update(type=name, initial=initial)
    assert DeviceConfig.model_validate(document).metrics[0].type is SnmpDataType.INTEGER32


@pytest.mark.parametrize("initial", [-(2**31) - 1, 2**31, True, 1.5, "5", None])
def test_rejects_invalid_integer_values(initial: object) -> None:
    document = device_document()
    document["metrics"][0]["initial"] = initial
    with pytest.raises(ValidationError):
        DeviceConfig.model_validate(document)


@pytest.mark.parametrize("field", ["name", "oid"])
def test_rejects_duplicate_metrics(field: str) -> None:
    document = device_document()
    other = deepcopy(document["metrics"][0])
    other.update(name="other", oid="1.3.6.1.4.1.55555.2.0")
    other[field] = document["metrics"][0][field]
    if field == "oid":
        other[field] = ".01.3.6.1.4.1.55555.1.1.0"
    document["metrics"].append(other)
    with pytest.raises(ValidationError, match="unique"):
        DeviceConfig.model_validate(document)


@pytest.mark.parametrize(
    "oid",
    [
        "",
        "1",
        "3.1",
        "1.40",
        "1.-1",
        "1..3",
        "1.3.",
        "..1.3",
        "1. 3",
        "1.+3",
        "1.٣",
        "1.3.4294967296",
    ],
)
def test_rejects_invalid_oids(oid: str) -> None:
    document = device_document()
    document["metrics"][0]["oid"] = oid
    with pytest.raises(ValidationError):
        DeviceConfig.model_validate(document)
    with pytest.raises(ValidationError):
        ScenarioDocument.model_validate(scenario_document(oid=oid))


@pytest.mark.parametrize(
    "field,value",
    [("type", "Unknown"), ("name", "  "), ("extra", 1), ("initial", {}), ("oid", 123)],
)
def test_rejects_invalid_metric_fields(field: str, value: object) -> None:
    document = device_document()
    document["metrics"][0][field] = value
    with pytest.raises(ValidationError):
        DeviceConfig.model_validate(document)


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration", 0),
        ("duration", -1),
        ("duration", True),
        ("duration", "1"),
        ("duration", float("inf")),
        ("update_interval", 0),
        ("update_interval", -0.1),
        ("update_interval", False),
        ("update_interval", float("nan")),
        ("extra", 1),
        ("rules", []),
    ],
)
def test_rejects_invalid_scenario_fields(field: str, value: object) -> None:
    document = scenario_document()
    document[field] = value
    with pytest.raises(ValidationError):
        ScenarioDocument.model_validate(document)


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "constant", "oid": OID, "value": True},
        {"type": "jump", "oid": OID, "at": 0.2, "value": 2**31},
        {"type": "step", "oid": OID, "step": 1, "limit": 2**31},
        {"type": "step", "oid": OID, "step": 1, "limit": 5},
        {"type": "step", "oid": OID, "step": -1, "limit": 20},
        {"type": "constant", "oid": "1.3.99", "value": 1},
    ],
)
def test_rejects_invalid_links_and_rule_values(rule: dict) -> None:
    document = scenario_document()
    document["rules"] = [rule]
    with pytest.raises(ValueError):
        bind_config(
            DeviceConfig.model_validate(device_document()),
            ScenarioDocument.model_validate(document),
        )


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "jump", "oid": OID, "at": -1, "value": 5},
        {"type": "jump", "oid": OID, "at": 2, "value": 5},
        {"type": "jump", "oid": OID, "at": True, "value": 5},
        {"type": "constant", "oid": OID},
        {"type": "step", "oid": OID, "step": 0, "limit": 20},
        {"type": "step", "oid": OID, "step": True, "limit": 20},
        {"type": "step", "oid": OID, "step": 1, "limit": 20, "extra": 1},
        {"type": "ramp", "oid": OID},
    ],
)
def test_rejects_invalid_rule_schema(rule: dict) -> None:
    document = scenario_document()
    document["rules"] = [rule]
    with pytest.raises(ValidationError):
        ScenarioDocument.model_validate(document)


def test_rejects_conflicting_normalized_rule_oids() -> None:
    document = scenario_document()
    document["rules"].append({"type": "constant", "oid": "." + OID, "value": 10})
    with pytest.raises(ValidationError, match="one rule per OID"):
        ScenarioDocument.model_validate(document)


def test_rejects_step_for_text_metric() -> None:
    document = device_document()
    document["metrics"][0].update(type="OctetString", initial="OK")
    with pytest.raises(ValueError, match="numeric"):
        bind_config(
            DeviceConfig.model_validate(document),
            ScenarioDocument.model_validate(scenario_document()),
        )


@pytest.mark.parametrize(
    "contents",
    [
        b"\xff",
        b"{",
        b'{"a":1,"a":2}',
        b'{"a":{"b":1,"b":2}}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b"\xef\xbb\xbf{}",
    ],
)
def test_rejects_invalid_json_and_encoding(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(contents)
    with pytest.raises(ConfigurationError):
        read_json(path)


def test_reports_file_and_schema_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot read"):
        load_json_config(tmp_path / "missing.json", EXAMPLES / "stable.json")
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"extra": 1}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Extra inputs"):
        load_json_config(EXAMPLES / "device.json", path)


@pytest.mark.parametrize("level", ["device", "agent"])
def test_rejects_unknown_device_fields(level: str) -> None:
    document = device_document()
    (document if level == "device" else document["agent"])["unknown"] = 1
    with pytest.raises(ValidationError, match="Extra inputs"):
        DeviceConfig.model_validate(document)


@pytest.mark.parametrize(
    "agent",
    [
        {"host": ""},
        {"host": "invalid"},
        {"host": "::1"},
        {"port": 0},
        {"port": 65536},
        {"port": True},
        {"port": "1161"},
        {"community": ""},
        {"community": "\ud800"},
    ],
)
def test_rejects_invalid_agent_values(agent: dict) -> None:
    document = device_document()
    document["agent"] = agent
    with pytest.raises(ValidationError):
        DeviceConfig.model_validate(document)
