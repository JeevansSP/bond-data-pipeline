"""File-level ETL funnel metrics collected by source connectors.

Each connector records one :class:`FileMetric` per raw artifact it pulls (a file, an API page, a
trade segment), capturing the extract -> transform funnel. The load count is recorded separately in
``ingestion_runs.rows_ingested``, so the two together describe the whole ETL pipeline per run.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileMetric:
    """Extract/transform metrics for one raw artifact (idempotent per artifact within a run)."""

    artifact: str
    """Stable name of the raw artifact, e.g. ``gsec/2026-07-10`` or ``page_0001``."""
    bytes_downloaded: int = 0
    rows_extracted: int = 0
    """Raw records seen in the artifact (before validity filtering)."""
    rows_parsed: int = 0
    """Records that parsed into valid domain objects."""
    rows_dropped: int = 0
    """Raw records skipped (blank/invalid/duplicate)."""


class MetricsCollector:
    """Mixin giving a connector a per-fetch list of :class:`FileMetric`.

    Connectors call :meth:`reset_metrics` at the start of each fetch and :meth:`add_metric` per
    artifact. Initialise by calling ``reset_metrics()`` in ``__init__``.
    """

    metrics: list[FileMetric]

    def reset_metrics(self) -> None:
        """Clear metrics at the start of a fetch."""
        self.metrics = []

    def add_metric(
        self,
        artifact: str,
        *,
        bytes_downloaded: int = 0,
        rows_extracted: int = 0,
        rows_parsed: int = 0,
        rows_dropped: int = 0,
    ) -> None:
        """Record the funnel metrics for one artifact."""
        self.metrics.append(
            FileMetric(
                artifact=artifact,
                bytes_downloaded=bytes_downloaded,
                rows_extracted=rows_extracted,
                rows_parsed=rows_parsed,
                rows_dropped=rows_dropped,
            )
        )
