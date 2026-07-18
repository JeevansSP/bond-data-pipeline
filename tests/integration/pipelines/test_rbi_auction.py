"""Integration tests for the RBI auction pipeline (needs Postgres; ``-m integration``)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, func, select

from bonds.models import RbiAuctionRecord
from bonds.pipelines import RbiAuctionPipeline, RunStatus
from bonds.storage import Database, IngestionRun, RbiAuction

pytestmark = pytest.mark.integration

SOURCE = "faketest"
AS_OF = dt.date(2026, 7, 18)


class FakeAuctionSource:
    """A source returning canned auction records (no network)."""

    name = SOURCE

    def fetch_auctions(self, as_of: dt.date) -> list[RbiAuctionRecord]:
        return [
            RbiAuctionRecord(
                prid="90001",
                title="Auction of 91-Day Treasury Bills",
                auction_type="T-Bill",
                source=SOURCE,
                auction_date=dt.date(2026, 7, 17),
            ),
            RbiAuctionRecord(
                prid="90002",
                title="Auction of State Government Securities",
                auction_type="SDL",
                source=SOURCE,
                auction_date=dt.date(2026, 7, 16),
            ),
        ]


@pytest.fixture
def database() -> Database:
    db = Database()
    db.create_all()
    return db


@pytest.fixture(autouse=True)
def _cleanup(database: Database) -> Iterator[None]:
    def wipe() -> None:
        with database.session() as s:
            s.execute(delete(RbiAuction).where(RbiAuction.source == SOURCE))
            s.execute(delete(IngestionRun).where(IngestionRun.source == SOURCE))

    wipe()
    yield
    wipe()


def test_run_persists_auctions(database: Database) -> None:
    result = RbiAuctionPipeline(database, source=FakeAuctionSource()).run(AS_OF)
    assert result.status is RunStatus.SUCCESS
    assert result.rows == 2
    with database.session() as s:
        types = (
            s.execute(select(RbiAuction.auction_type).where(RbiAuction.source == SOURCE))
            .scalars()
            .all()
        )
    assert set(types) == {"T-Bill", "SDL"}


def test_run_is_idempotent(database: Database) -> None:
    pipeline = RbiAuctionPipeline(database, source=FakeAuctionSource())
    pipeline.run(AS_OF)
    pipeline.run(AS_OF)
    with database.session() as s:
        count = s.execute(
            select(func.count()).select_from(RbiAuction).where(RbiAuction.source == SOURCE)
        ).scalar_one()
    assert count == 2  # upsert, not duplicate
