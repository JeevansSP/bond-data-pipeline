"""Shared pipeline result types and the audited-run executor."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from bonds.logging import get_logger
from bonds.quality import MetricsCollector
from bonds.sources.base import DataUnavailable
from bonds.storage import Database
from bonds.storage.repositories import EtlMetricsRepository, IngestionRunRepository

logger = get_logger(__name__)


def persist_file_metrics(
    session: Session, source_obj: object, *, source: str, dataset: str, run_date: dt.date
) -> None:
    """Persist a connector's per-artifact ETL funnel metrics, if it collects any."""
    if isinstance(source_obj, MetricsCollector):
        EtlMetricsRepository(session).upsert(
            source=source, dataset=dataset, run_date=run_date, metrics=source_obj.metrics
        )


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


def execute_run(
    database: Database,
    *,
    source: str,
    dataset: str,
    run_date: dt.date,
    work: Callable[[Session], int],
) -> PipelineResult:
    """Run ``work`` (fetch + quality + persist) inside a session, audited and failure-safe.

    Guarantees, regardless of where an error occurs (fetch OR the DB write phase):
      * the ``ingestion_runs`` audit row is always written — on failure via a *fresh* session so
        it survives the rollback of the work transaction;
      * the run is idempotent per ``(source, dataset, run_date)`` (re-running a day overwrites);
      * this function never raises — it returns a ``PipelineResult`` with the terminal status.

    ``work`` returns the number of rows loaded and may raise :class:`DataUnavailable` (-> SKIPPED)
    or any other exception (-> FAILED).
    """
    started = dt.datetime.now(dt.UTC)
    try:
        with database.session() as session:
            rows = work(session)
            IngestionRunRepository(session).record(
                source=source,
                dataset=dataset,
                run_date=run_date,
                status=RunStatus.SUCCESS.value,
                rows=rows,
                started_at=started,
            )
        logger.info("pipeline.success", dataset=dataset, run_date=run_date.isoformat(), rows=rows)
        return PipelineResult(run_date, dataset, RunStatus.SUCCESS, rows=rows)
    except DataUnavailable as exc:
        _finalize(database, source, dataset, run_date, RunStatus.SKIPPED, str(exc), started)
        logger.info("pipeline.skipped", dataset=dataset, run_date=run_date.isoformat())
        return PipelineResult(run_date, dataset, RunStatus.SKIPPED, message=str(exc))
    except Exception as exc:
        _finalize(database, source, dataset, run_date, RunStatus.FAILED, repr(exc), started)
        logger.error(
            "pipeline.failed", dataset=dataset, run_date=run_date.isoformat(), error=repr(exc)
        )
        return PipelineResult(run_date, dataset, RunStatus.FAILED, message=repr(exc))


def _finalize(
    database: Database,
    source: str,
    dataset: str,
    run_date: dt.date,
    status: RunStatus,
    message: str,
    started: dt.datetime,
) -> None:
    """Write a terminal audit row in a fresh session (survives the work session's rollback)."""
    with database.session() as session:
        IngestionRunRepository(session).record(
            source=source,
            dataset=dataset,
            run_date=run_date,
            status=status.value,
            rows=0,
            started_at=started,
            message=message,
        )
