"""QualityInspector — runs batch checks + a DB drift check, persists and logs results."""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from bonds.logging import get_logger
from bonds.models import SecurityRecord, SovereignValuation
from bonds.quality.checks import Level, QualityCheck, check_universe, check_valuations
from bonds.storage.repositories import DataQualityRepository, IngestionRunRepository
from bonds.storage.schema import DataQualityCheck

logger = get_logger(__name__)

DRIFT_DROP_THRESHOLD = 0.20
"""Warn if a batch is >20% smaller than the previous successful run (possible truncated feed)."""


class QualityInspector:
    """Evaluates and persists data-quality checks for one dataset/run."""

    def __init__(self, session: Session, *, source: str, dataset: str, run_date: dt.date) -> None:
        self._session = session
        self._source = source
        self._dataset = dataset
        self._run_date = run_date

    def inspect_valuations(self, valuations: list[SovereignValuation]) -> list[QualityCheck]:
        """Run valuation checks + drift, persist, and log."""
        checks = check_valuations(valuations)
        checks.append(self._drift_check(len(valuations)))
        self._persist(checks)
        return checks

    def inspect_universe(self, records: list[SecurityRecord]) -> list[QualityCheck]:
        """Run universe checks + drift, persist, and log."""
        checks = check_universe(records, as_of=self._run_date)
        checks.append(self._drift_check(len(records)))
        self._persist(checks)
        return checks

    # ------------------------------------------------------------------ internals
    def _drift_check(self, current_rows: int) -> QualityCheck:
        previous = IngestionRunRepository(self._session).previous_row_count(
            self._dataset, before=self._run_date
        )
        if not previous:
            return QualityCheck(
                "row_count_drift", Level.INFO, passed=True, observed=0.0, detail="no prior run"
            )
        drop_rate = max(0.0, (previous - current_rows) / previous)
        return QualityCheck(
            "row_count_drift",
            Level.WARN,
            passed=drop_rate <= DRIFT_DROP_THRESHOLD,
            observed=drop_rate,
            detail=f"prev={previous} current={current_rows}",
        )

    def _persist(self, checks: list[QualityCheck]) -> None:
        rows = [
            DataQualityCheck(
                source=self._source,
                dataset=self._dataset,
                run_date=self._run_date,
                check_name=c.name,
                level=c.level.value,
                passed=c.passed,
                observed=c.observed,
                detail=c.detail,
            )
            for c in checks
        ]
        DataQualityRepository(self._session).record(rows)
        for c in checks:
            if c.passed:
                continue
            log = logger.error if c.level is Level.ERROR else logger.warning
            log(
                "quality.check_failed",
                dataset=self._dataset,
                check=c.name,
                level=c.level.value,
                observed=c.observed,
                detail=c.detail,
            )
