"""Shared pipeline result types."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum


class RunStatus(StrEnum):
    """Terminal status of a single dataset/date ingestion."""

    SUCCESS = "success"
    """Data was fetched and written."""
    SKIPPED = "skipped"
    """No data for that date (holiday/weekend) — expected, non-fatal."""
    FAILED = "failed"
    """An unexpected error occurred."""


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Outcome of ingesting one dataset for one business date."""

    date: dt.date
    dataset: str
    status: RunStatus
    rows: int = 0
    message: str | None = None
