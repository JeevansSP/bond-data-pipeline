"""QualityInspector — runs batch checks + a DB drift check, persists and logs results."""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from bonds.logging import get_logger
from bonds.models import (
    PublicIssueRecord,
    RbiAuctionRecord,
    SecurityRecord,
    SovereignValuation,
    TradeRecord,
)
from bonds.quality.checks import (
    Level,
    QualityCheck,
    check_public_issues,
    check_rbi_auctions,
    check_trades,
    check_universe,
    check_valuations,
)
from bonds.storage.repositories import (
    DataQualityRepository,
    IngestionRunRepository,
    SecurityRepository,
)
from bonds.storage.schema import DataQualityCheck

logger = get_logger(__name__)

DRIFT_DROP_THRESHOLD = 0.20
"""Warn if a batch is >20% smaller than the previous successful run (possible truncated feed)."""
COUPON_TOLERANCE = 0.001
"""Coupons closer than this are treated as equal across sources."""


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
        """Run universe checks + drift + cross-source reconciliation, persist, and log.

        Reconciliation must run BEFORE the upsert overwrites the stored row, so the comparison is
        against whatever *other* source last wrote the ISIN.
        """
        checks = check_universe(records, as_of=self._run_date)
        checks.append(self._drift_check(len(records)))
        checks += self._reconcile(records)
        self._persist(checks)
        return checks

    def inspect_public_issues(self, issues: list[PublicIssueRecord]) -> list[QualityCheck]:
        """Run public-issue checks + drift, persist, and log."""
        checks = check_public_issues(issues)
        checks.append(self._drift_check(len(issues)))
        self._persist(checks)
        return checks

    def inspect_rbi_auctions(self, auctions: list[RbiAuctionRecord]) -> list[QualityCheck]:
        """Run RBI auction checks + drift, persist, and log."""
        checks = check_rbi_auctions(auctions)
        checks.append(self._drift_check(len(auctions)))
        self._persist(checks)
        return checks

    def inspect_trades(self, trades: list[TradeRecord]) -> list[QualityCheck]:
        """Run trade checks (no drift — trade counts vary widely by session), persist, and log."""
        checks = check_trades(trades)
        self._persist(checks)
        return checks

    def _reconcile(self, records: list[SecurityRecord]) -> list[QualityCheck]:
        stored = SecurityRepository(self._session).load_reference([r.isin for r in records])
        compared = coupon_mismatch = maturity_mismatch = 0
        for r in records:
            existing = stored.get(r.isin)
            if existing is None:
                continue
            ex_coupon, ex_maturity, ex_source = existing
            if ex_source == r.source:
                continue  # same source re-ingest, not a cross-source comparison
            compared += 1
            if (
                r.coupon is not None
                and ex_coupon is not None
                and abs(r.coupon - ex_coupon) > COUPON_TOLERANCE
            ):
                coupon_mismatch += 1
            if (
                r.maturity_date is not None
                and ex_maturity is not None
                and r.maturity_date != ex_maturity
            ):
                maturity_mismatch += 1
        detail = f"compared {compared} ISINs vs another source"
        return [
            QualityCheck(
                "cross_source_coupon_mismatch",
                Level.WARN,
                passed=coupon_mismatch == 0,
                observed=float(coupon_mismatch),
                detail=detail,
            ),
            QualityCheck(
                "cross_source_maturity_mismatch",
                Level.WARN,
                passed=maturity_mismatch == 0,
                observed=float(maturity_mismatch),
                detail=detail,
            ),
        ]

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
