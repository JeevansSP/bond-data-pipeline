"""Integration tests for the securities enrichment pipeline (needs Postgres).

Uses sentinel ISINs and source ``faketest`` so it is non-destructive; the fake fetcher returns
``None`` for any real ISIN the missing-coupon query may also select from the live database.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select

from bonds.models import InstrumentType, SecurityRecord
from bonds.pipelines.base import RunStatus
from bonds.pipelines.enrichment import EnrichmentPipeline
from bonds.sources.base import SourceError
from bonds.storage import Database, IngestionRun, Security
from bonds.storage.repositories import SecurityRepository

pytestmark = pytest.mark.integration

SOURCE = "faketest"
ISIN_FILLED = "INTEST000101"
ISIN_FETCH_FAILS = "INTEST000102"
ISIN_NOT_COVERED = "INTEST000103"
DAY = dt.date(2026, 7, 17)


class FakeFetcher:
    """A :class:`ReferenceFetcher` with canned per-ISIN behaviour (no network)."""

    name = SOURCE

    def fetch_reference(self, isin: str) -> SecurityRecord | None:
        if isin == ISIN_FETCH_FAILS:
            raise SourceError("boom")
        if isin == ISIN_FILLED:
            return SecurityRecord(
                isin=isin,
                instrument_type=InstrumentType.CORP,
                source=SOURCE,
                coupon=9.15,
                maturity_date=dt.date(2031, 3, 31),
                issuer="Fetched Issuer",
            )
        return None  # not covered (including any real DB rows the query selects)


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database()
    db.create_all()

    def wipe() -> None:
        with db.session() as s:
            s.execute(delete(Security).where(Security.source == SOURCE))
            s.execute(delete(IngestionRun).where(IngestionRun.source == SOURCE))

    wipe()
    yield db
    wipe()


def _seed(db: Database) -> None:
    records = [
        SecurityRecord(
            isin=isin, instrument_type=InstrumentType.CORP, source=SOURCE, issuer="Seed Issuer"
        )
        for isin in (ISIN_FILLED, ISIN_FETCH_FAILS, ISIN_NOT_COVERED)
    ]
    with db.session() as s:
        SecurityRepository(s).upsert_many(records, seen_on=DAY)


def test_enrichment_fills_gaps_and_survives_fetch_failures(database: Database) -> None:
    _seed(database)
    result = EnrichmentPipeline(database, source=FakeFetcher()).run(DAY)

    # One ISIN fetch-failed and one wasn't covered, but the run still succeeds.
    assert result.status is RunStatus.SUCCESS
    assert result.dataset == f"{SOURCE}.enrichment"

    with database.session() as s:
        rows = {
            r.isin: r
            for r in s.execute(select(Security).where(Security.source == SOURCE)).scalars()
        }
    assert rows[ISIN_FILLED].coupon == pytest.approx(9.15)
    assert rows[ISIN_FILLED].maturity_date == dt.date(2031, 3, 31)
    assert rows[ISIN_FILLED].issuer == "Seed Issuer"  # existing value never overwritten
    assert rows[ISIN_FETCH_FAILS].coupon is None  # failed fetch -> left as-is
    assert rows[ISIN_NOT_COVERED].coupon is None  # not covered -> left as-is

    with database.session() as s:
        run = s.execute(
            select(IngestionRun).where(IngestionRun.dataset == f"{SOURCE}.enrichment")
        ).scalar_one()
    assert run.status == "success"


def test_enrichment_is_idempotent_for_filled_rows(database: Database) -> None:
    _seed(database)
    pipeline = EnrichmentPipeline(database, source=FakeFetcher())
    first = pipeline.run(DAY)
    second = pipeline.run(DAY)
    assert first.status is RunStatus.SUCCESS and second.status is RunStatus.SUCCESS
    # The filled ISIN no longer matches the missing-coupon query on the second pass, so the
    # second run touches strictly fewer rows than the first.
    assert second.rows <= first.rows
    with database.session() as s:
        row = s.execute(select(Security).where(Security.isin == ISIN_FILLED)).scalar_one()
    assert row.coupon == pytest.approx(9.15)
