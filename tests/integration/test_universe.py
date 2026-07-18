"""Integration tests for the universe pipeline (needs Postgres; ``-m integration``).

Uses sentinel ISINs (``INUNIV______``) and source ``fakeuniv`` so it is non-destructive.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select

from bonds.models import InstrumentType, SecurityRecord
from bonds.pipelines import RunStatus, UniversePipeline
from bonds.storage import Database, IngestionRun, Security, SecurityAttributeHistory
from bonds.storage.schema import Valuation

pytestmark = pytest.mark.integration

SOURCE = "fakeuniv"
ISIN_A = "INUNIV000001"
ISIN_B = "INUNIV000002"
DAY1 = dt.date(2026, 7, 10)
DAY2 = dt.date(2026, 7, 17)


class FakeUniverseSource:
    """A :class:`UniverseFetcher` yielding canned records (no network)."""

    name = SOURCE

    def __init__(self, records: list[SecurityRecord]) -> None:
        self._records = records

    def iter_records(
        self, as_of: dt.date, *, size: int = 100, max_pages: int | None = None
    ) -> Iterator[SecurityRecord]:
        yield from self._records


def _rec(isin: str, rating: str | None) -> SecurityRecord:
    return SecurityRecord(
        isin=isin,
        instrument_type=InstrumentType.CORP,
        source=SOURCE,
        issuer="ACME LTD",
        coupon=7.0,
        attributes={"credit_rating": rating, "security_status": "ACTIVE"},
    )


@pytest.fixture
def database() -> Database:
    db = Database()
    db.create_all()
    return db


@pytest.fixture(autouse=True)
def _cleanup(database: Database) -> Iterator[None]:
    def wipe() -> None:
        with database.session() as s:
            s.execute(delete(Valuation).where(Valuation.source == SOURCE))
            s.execute(delete(Security).where(Security.source == SOURCE))
            s.execute(
                delete(SecurityAttributeHistory).where(SecurityAttributeHistory.source == SOURCE)
            )
            s.execute(delete(IngestionRun).where(IngestionRun.source == SOURCE))

    wipe()
    yield
    wipe()


def _rating_history(database: Database, isin: str) -> list[tuple[str | None, dt.date | None]]:
    with database.session() as s:
        rows = (
            s.execute(
                select(SecurityAttributeHistory)
                .where(
                    SecurityAttributeHistory.isin == isin,
                    SecurityAttributeHistory.attribute == "credit_rating",
                )
                .order_by(SecurityAttributeHistory.valid_from)
            )
            .scalars()
            .all()
        )
    return [(r.value, r.valid_to) for r in rows]


def test_upserts_universe_and_records_ratings(database: Database) -> None:
    source = FakeUniverseSource([_rec(ISIN_A, "AAA"), _rec(ISIN_B, "AA+")])
    result = UniversePipeline(database, source=source).run(DAY1)

    assert result.status is RunStatus.SUCCESS
    assert result.rows == 2
    with database.session() as s:
        secs = s.execute(select(Security).where(Security.source == SOURCE)).scalars().all()
    assert {sec.isin for sec in secs} == {ISIN_A, ISIN_B}
    assert _rating_history(database, ISIN_A) == [("AAA", None)]


def test_rerun_is_idempotent_no_spurious_changes(database: Database) -> None:
    source = FakeUniverseSource([_rec(ISIN_A, "AAA"), _rec(ISIN_B, "AA+")])
    pipeline = UniversePipeline(database, source=source)
    pipeline.run(DAY1)
    pipeline.run(DAY1)  # same snapshot again
    assert _rating_history(database, ISIN_A) == [("AAA", None)]  # still one row


def test_rating_downgrade_is_recorded_as_scd2(database: Database) -> None:
    UniversePipeline(database, source=FakeUniverseSource([_rec(ISIN_A, "AAA")])).run(DAY1)
    UniversePipeline(database, source=FakeUniverseSource([_rec(ISIN_A, "AA")])).run(DAY2)

    assert _rating_history(database, ISIN_A) == [
        ("AAA", DAY2 - dt.timedelta(days=1)),  # closed the day before the change
        ("AA", None),  # current
    ]
