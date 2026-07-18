"""Integration tests for SecurityRepository.insert_missing (needs Postgres)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select

from bonds.models import InstrumentType, SecurityRecord
from bonds.storage import Database, Security
from bonds.storage.repositories import SecurityRepository

pytestmark = pytest.mark.integration

ISIN = "INSECTEST001"  # sentinel -> non-destructive
DAY = dt.date(2026, 7, 18)


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database()
    database.create_all()

    def _clean() -> None:
        with database.session() as s:
            s.execute(delete(Security).where(Security.isin == ISIN))

    _clean()
    yield database
    _clean()


def _sec(source: str, itype: InstrumentType, coupon: float | None) -> SecurityRecord:
    return SecurityRecord(isin=ISIN, instrument_type=itype, source=source, coupon=coupon)


def test_insert_missing_inserts_then_does_not_overwrite(db: Database) -> None:
    # First insert (as if from FBIL) writes the row.
    with db.session() as s:
        SecurityRepository(s).insert_missing([_sec("fbil", InstrumentType.GSEC, 6.94)], seen_on=DAY)
    # A later CCIL-derived insert for the SAME ISIN must NOT overwrite the authoritative row.
    with db.session() as s:
        SecurityRepository(s).insert_missing([_sec("ccil", InstrumentType.TBILL, 0.0)], seen_on=DAY)
    with db.session() as s:
        row = s.execute(select(Security).where(Security.isin == ISIN)).scalar_one()
        assert row.source == "fbil"  # unchanged
        assert row.instrument_type == "GSEC"
        assert row.coupon == pytest.approx(6.94)
