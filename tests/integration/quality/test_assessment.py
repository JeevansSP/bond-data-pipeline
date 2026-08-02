"""Integration tests for the DB-wide assessment (needs Postgres)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete

from bonds.models import TradeRecord
from bonds.quality.assessment import check_referential_integrity, run_assessment
from bonds.quality.checks import Level
from bonds.storage import Database
from bonds.storage.repositories import TradeRepository
from bonds.storage.schema import Trade

pytestmark = pytest.mark.integration

ORPHAN_ISIN = "INASSESS0001"  # sentinel corp trade with no securities row


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database()
    database.create_all()

    def _clean() -> None:
        with database.session() as s:
            s.execute(delete(Trade).where(Trade.isin == ORPHAN_ISIN))

    _clean()
    yield database
    _clean()


def test_run_assessment_returns_all_dimensions_no_error(db: Database) -> None:
    report = run_assessment(db)
    assert set(report.groups) == {
        "Uniqueness",
        "Referential integrity",
        "Completeness",
        "Consistency",
        "Cross-source reconciliation",
    }
    assert not report.has_error  # the loaded warehouse is clean


def test_referential_check_catches_orphan_corp_trade(db: Database) -> None:
    with db.session() as s:
        TradeRepository(s).upsert_many(
            [
                TradeRecord(
                    isin=ORPHAN_ISIN,
                    trade_date=dt.date(2026, 7, 17),
                    source="nse",
                    segment="otctrades_listed",
                    ltp=101.0,
                )
            ]
        )
    with db.engine.connect() as conn:
        checks = {c.name: c for c in check_referential_integrity(conn)}
    orphan = checks["orphan_corp_trades"]
    assert orphan.observed is not None and orphan.observed >= 1
    assert orphan.level is Level.WARN and not orphan.passed
    # a corporate orphan must NOT trip the sovereign ERROR check
    assert checks["orphan_sovereign_trades"].passed


def test_consistency_checks_report(db: Database) -> None:
    from bonds.quality.assessment import check_consistency

    with db.engine.connect() as conn:
        checks = {c.name: c for c in check_consistency(conn)}
    assert set(checks) == {
        "zero_coupon_contradiction",
        "strips_with_nonzero_coupon",
        "interest_type_noncanonical",
        "implausible_trade_yields",
        "matured_but_status_active",
        "coupon_above_25pct",
        "curve_sparse_days",
        "corp_trade_price_scale_outliers",
        "corp_trade_extreme_yields",
    }
    # INFO rows are observations and always pass.
    assert checks["matured_but_status_active"].passed
    assert checks["coupon_above_25pct"].passed
    assert checks["corp_trade_price_scale_outliers"].passed
    assert checks["corp_trade_extreme_yields"].passed
    # Regression guards for the interest_type/coupon normalization: presence + level only —
    # their passed status depends on whether the repair migration has been applied.
    assert checks["strips_with_nonzero_coupon"].level is Level.ERROR
    assert checks["interest_type_noncanonical"].level is Level.ERROR


def test_completeness_checks_include_sovereign_face_value(db: Database) -> None:
    from bonds.quality.assessment import check_completeness

    with db.engine.connect() as conn:
        checks = {c.name: c for c in check_completeness(conn)}
    assert "sovereign_missing_face_value" in checks
    assert checks["sovereign_missing_face_value"].level is Level.WARN
