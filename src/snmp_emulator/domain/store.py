from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import replace

from .metric import Metric
from .oid import ObjectIdentifier


class MetricStore:
    def __init__(self, metrics: Iterable[Metric]) -> None:
        metric_list = list(metrics)
        self._by_oid = {metric.oid: metric for metric in metric_list}
        self._by_name = {metric.name: metric for metric in metric_list}
        if len(self._by_oid) != len(metric_list):
            raise ValueError("metric OIDs must be unique")
        if len(self._by_name) != len(metric_list):
            raise ValueError("metric names must be unique")
        self._ordered_oids = sorted(self._by_oid)

    def get(self, oid: ObjectIdentifier) -> Metric | None:
        return self._by_oid.get(oid)

    def get_by_name(self, name: str) -> Metric | None:
        return self._by_name.get(name)

    def get_next(self, oid: ObjectIdentifier) -> Metric | None:
        index = bisect_right(self._ordered_oids, oid)
        if index == len(self._ordered_oids):
            return None
        return self._by_oid[self._ordered_oids[index]]

    def update(self, name: str, value: object) -> None:
        metric = self.get_by_name(name)
        if metric is None:
            raise KeyError(name)
        metric.update(value)

    def snapshot(self) -> tuple[Metric, ...]:
        return tuple(replace(self._by_oid[oid]) for oid in self._ordered_oids)

    def __len__(self) -> int:
        return len(self._ordered_oids)
