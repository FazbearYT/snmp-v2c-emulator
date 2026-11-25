import json
import os
from pathlib import Path
from stat import S_IMODE

import pytest

from snmp_emulator.config import ConfigurationError
from snmp_emulator.config_editor import (
    EditorValidationError,
    read_device,
    read_scenario,
    save_pair,
    validate_pair,
)
from snmp_emulator.json_config import load_json_config

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.mark.parametrize(
    "device_name,scenario_name",
    [
        ("device", "stable"),
        ("device", "temperature-jump"),
        ("device", "cpu-step"),
        ("device-10", "variable-load"),
        ("device-emergency", "recovery"),
        ("device-normal", "device-unavailability"),
        ("device-normal", "partial-oid-unavailability"),
    ],
)
def test_examples_save_and_reopen(tmp_path: Path, device_name: str, scenario_name: str) -> None:
    original_device = EXAMPLES / f"{device_name}.json"
    original_scenario = EXAMPLES / f"{scenario_name}.json"
    device = read_device(original_device)
    scenario = read_scenario(original_scenario)
    device_path = tmp_path / "device.json"
    scenario_path = tmp_path / "scenario.json"

    save_pair(device_path, scenario_path, device, scenario)

    assert read_device(device_path) == device
    assert read_scenario(scenario_path) == scenario
    assert load_json_config(device_path, scenario_path) == load_json_config(
        original_device, original_scenario
    )


def test_saved_edits_reach_shared_loader(tmp_path: Path) -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    device["metrics"][0]["initial"] = 23
    device["metrics"][2]["initial"] = "Устройство"
    scenario["rules"][0]["step"] = 7
    scenario["rules"][0]["limit"] = 72
    device_path = tmp_path / "device.json"
    scenario_path = tmp_path / "scenario.json"

    save_pair(device_path, scenario_path, device, scenario)

    config = load_json_config(device_path, scenario_path)
    assert config.metrics[0].initial == 23
    assert config.metrics[2].initial == "Устройство"
    assert "Устройство" in device_path.read_text(encoding="utf-8")
    action = config.scenarios[0].actions[0]
    assert (action.start, action.amount, action.maximum) == (23, 7, 72)


def test_save_validates_before_replacing_existing_files(tmp_path: Path) -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    scenario["rules"][0]["step"] = 0
    device_path = tmp_path / "device.json"
    scenario_path = tmp_path / "scenario.json"
    for path in (device_path, scenario_path):
        path.write_text("original", encoding="utf-8")

    with pytest.raises(EditorValidationError) as exc:
        save_pair(device_path, scenario_path, device, scenario)

    issue = exc.value.issues[0]
    assert (issue.document, issue.field) == ("scenario", ("rules", 0, "step"))
    assert device_path.read_text(encoding="utf-8") == "original"
    assert scenario_path.read_text(encoding="utf-8") == "original"


def test_invalid_agent_address_points_to_host_field() -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    device["agent"]["host"] = "localhost"

    with pytest.raises(EditorValidationError) as exc:
        validate_pair(device, scenario)

    issue = exc.value.issues[0]
    assert (issue.document, issue.field) == ("device", ("agent", "host"))


def test_jump_after_scenario_end_points_to_its_time_field() -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "temperature-jump.json")
    rule = next(rule for rule in scenario["rules"] if rule["type"] == "jump")
    rule["at"] = scenario["duration"] + 1

    with pytest.raises(EditorValidationError, match="jump at must not exceed duration") as exc:
        validate_pair(device, scenario)

    assert exc.value.issues[0].field == ("rules", scenario["rules"].index(rule), "at")


def test_unknown_oid_and_invalid_value_point_to_matching_rules() -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    scenario["rules"][0]["oid"] = "1.3.6.1.4.1.55555.8.8.0"
    description_oid = device["metrics"][2]["oid"]
    scenario["rules"].append({"type": "constant", "oid": description_oid, "value": 42})
    scenario["oid_responses"] = [{"oid": "1.3.99", "mode": "genErr"}]

    with pytest.raises(EditorValidationError) as exc:
        validate_pair(device, scenario)

    assert [issue.field for issue in exc.value.issues] == [
        ("rules", 0, "oid"),
        ("rules", 1, "value"),
        ("oid_responses", 0, "oid"),
    ]


def test_open_rejects_unrecognized_data_without_changing_file(tmp_path: Path) -> None:
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    scenario["rules"][0]["future_option"] = True
    path = tmp_path / "scenario.json"
    original = json.dumps(scenario).encode()
    path.write_bytes(original)

    with pytest.raises(EditorValidationError, match="не поддерживается") as exc:
        read_scenario(path)

    assert exc.value.issues[0].field == ("rules", 0, "future_option")
    assert path.read_bytes() == original


def test_save_rejects_the_same_destination(tmp_path: Path) -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    path = tmp_path / "same.json"

    with pytest.raises(ConfigurationError, match="разные файлы"):
        save_pair(path, path, device, scenario)

    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_save_preserves_existing_file_modes(tmp_path: Path) -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    device_path = tmp_path / "device.json"
    scenario_path = tmp_path / "scenario.json"
    for path, mode in ((device_path, 0o600), (scenario_path, 0o640)):
        path.touch()
        path.chmod(mode)

    save_pair(device_path, scenario_path, device, scenario)

    assert S_IMODE(device_path.stat().st_mode) == 0o600
    assert S_IMODE(scenario_path.stat().st_mode) == 0o640


def test_new_paths_may_alias_on_the_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    device_path = tmp_path.resolve() / "device.json"
    scenario_path = tmp_path.resolve() / "alias.json"
    exists = Path.exists
    samefile = Path.samefile

    def path_exists(path: Path) -> bool:
        return exists(device_path if path == scenario_path else path)

    def same_file(path: Path, other: Path) -> bool:
        return (path == device_path and other == scenario_path) or samefile(path, other)

    monkeypatch.setattr(Path, "exists", path_exists)
    monkeypatch.setattr(Path, "samefile", same_file)

    with pytest.raises(ConfigurationError, match="разные файлы"):
        save_pair(device_path, scenario_path, device, scenario)

    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("device_already_exists", [False, True])
def test_failed_second_replace_restores_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, device_already_exists: bool
) -> None:
    device = read_device(EXAMPLES / "device.json")
    scenario = read_scenario(EXAMPLES / "cpu-step.json")
    device_path = tmp_path / "device.json"
    scenario_path = tmp_path / "scenario.json"
    if device_already_exists:
        device_path.write_text("old device", encoding="utf-8")
    scenario_path.write_text("old scenario", encoding="utf-8")
    replace = Path.replace

    def replace_path(source: Path, target: Path) -> Path:
        if target == scenario_path:
            raise PermissionError("test write failure")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_path)
    with pytest.raises(ConfigurationError, match="test write failure"):
        save_pair(device_path, scenario_path, device, scenario)

    assert device_path.exists() == device_already_exists
    if device_already_exists:
        assert device_path.read_text(encoding="utf-8") == "old device"
    assert scenario_path.read_text(encoding="utf-8") == "old scenario"
