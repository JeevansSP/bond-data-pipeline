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
