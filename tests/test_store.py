import pytest

from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType


def metric(name: str, oid: str, value: int) -> Metric:
    return Metric(name, ObjectIdentifier.parse(oid), SnmpDataType.GAUGE32, value)


def test_exact_and_next_lookup() -> None:
    store = MetricStore(
        [
            metric("third", "1.3.6.1.4.1.55555.3.0", 3),
            metric("first", "1.3.6.1.4.1.55555.1.0", 1),
        ]
    )

    assert store.get(ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")).name == "first"
    assert store.get_next(ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")).name == "third"
    assert store.get_next(ObjectIdentifier.parse("1.3.6.1.4.1.55555.3.0")) is None


def test_update_preserves_metric_type() -> None:
    store = MetricStore([metric("cpu", "1.3.6.1.4.1.55555.1.0", 10)])

    store.update("cpu", 90)

    assert store.get_by_name("cpu").value == 90
    with pytest.raises(ValueError):
        store.update("cpu", -1)


def test_snapshot_is_isolated_from_later_updates() -> None:
    store = MetricStore([metric("cpu", "1.3.6.1.4.1.55555.1.0", 10)])

    snapshot = store.snapshot()
    store.update("cpu", 90)
    snapshot[0].value = 25

    assert snapshot[0].value == 25
    assert store.get_by_name("cpu").value == 90
