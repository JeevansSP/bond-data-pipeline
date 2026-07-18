"""Tests for the ETL file-metrics collector."""

from __future__ import annotations

from bonds.quality.metrics import FileMetric, MetricsCollector


class _Collector(MetricsCollector):
    def __init__(self) -> None:
        self.reset_metrics()


def test_reset_and_add_metric() -> None:
    c = _Collector()
    assert c.metrics == []
    c.add_metric("a", bytes_downloaded=10, rows_extracted=5, rows_parsed=4, rows_dropped=1)
    c.add_metric("b", bytes_downloaded=20, rows_parsed=2)
    assert c.metrics == [
        FileMetric("a", bytes_downloaded=10, rows_extracted=5, rows_parsed=4, rows_dropped=1),
        FileMetric("b", bytes_downloaded=20, rows_extracted=0, rows_parsed=2, rows_dropped=0),
    ]


def test_reset_clears_previous_run() -> None:
    c = _Collector()
    c.add_metric("a", rows_parsed=1)
    c.reset_metrics()
    assert c.metrics == []
