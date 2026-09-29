from pathlib import Path

import pytest

from snmp_emulator.config import ConfigurationError, load_config


def test_loads_example_configuration() -> None:
    config = load_config(Path(__file__).parents[1] / "examples" / "device.yaml")

    assert config.agent.port == 1161
    assert config.agent.community == "public"
    assert [metric.name for metric in config.metrics] == [
        "system_description",
        "cpu_usage",
    ]


def test_rejects_duplicate_oids(tmp_path: Path) -> None:
    config_path = tmp_path / "duplicate.yaml"
    config_path.write_text(
        """
schema_version: 1
metrics:
  - {name: first, oid: 1.3.6.1.2.1.1.1.0, type: Integer, initial: 1}
  - {name: second, oid: 1.3.6.1.2.1.1.1.0, type: Integer, initial: 2}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unique"):
        load_config(config_path)
