from pysnmp.proto import rfc1902

from snmp_emulator.adapters.pysnmp_agent import StoreInstrumentation
from snmp_emulator.domain.metric import Metric
from snmp_emulator.domain.oid import ObjectIdentifier
from snmp_emulator.domain.store import MetricStore
from snmp_emulator.domain.types import SnmpDataType


def test_reads_existing_metric() -> None:
    oid = ObjectIdentifier.parse("1.3.6.1.4.1.55555.1.0")
    store = MetricStore([Metric("cpu", oid, SnmpDataType.GAUGE32, 25)])
    instrumentation = StoreInstrumentation(store)

    result = instrumentation.read_variables((rfc1902.ObjectName(oid.parts), rfc1902.Null("")))

    assert int(result[0][1]) == 25
    assert result[0][1].tagSet == rfc1902.Gauge32.tagSet
