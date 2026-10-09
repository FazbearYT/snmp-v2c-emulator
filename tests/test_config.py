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


def write_config(tmp_path: Path, scenario: str) -> Path:
    config_path = tmp_path / "scenario.yaml"
    config_path.write_text(
        f"""
schema_version: 1
metrics:
  - {{name: cpu, oid: 1.3.6.1.4.1.55555.1.0, type: Gauge32, initial: 10}}
scenarios:
  - name: test
{scenario}
""",
        encoding="utf-8",
    )
    return config_path


def test_rejects_fractional_ramp_for_integer_snmp_metric(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """    actions:
      - {type: ramp, metric: cpu, duration: 10, start: 10.5, end: 20}
""",
    )

    with pytest.raises(ConfigurationError, match="fractional"):
        load_config(config_path)


def test_rejects_unbounded_non_repeating_step(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """    actions:
      - {type: step, metric: cpu, interval: 1, start: 10, amount: 1}
""",
    )

    with pytest.raises(ConfigurationError, match="requires maximum"):
        load_config(config_path)


def test_accepts_bounded_non_repeating_step(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """    actions:
      - {type: step, metric: cpu, interval: 1, start: 10, amount: 1, maximum: 100}
""",
    )

    config = load_config(config_path)

    assert config.scenarios[0].actions[0].maximum == 100


def test_rejects_action_that_never_runs_in_repeating_scenario(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """    repeat_every: 5
    actions:
      - {type: set, metric: cpu, at: 5, value: 20}
""",
    )

    with pytest.raises(ConfigurationError, match="must be less than repeat_every"):
        load_config(config_path)


def test_rejects_inverted_step_limits(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """    actions:
      - {type: step, metric: cpu, interval: 1, start: 10, amount: 1, minimum: 50, maximum: 40}
""",
    )

    with pytest.raises(ConfigurationError, match="minimum cannot exceed maximum"):
        load_config(config_path)


def test_reports_invalid_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text("metrics: [", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="invalid YAML"):
        load_config(config_path)
