"""Integration tests for the trade pipeline (needs Postgres; ``-m integration``)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, func, select

from bonds.models import TradeRecord
from bonds.pipelines import RunStatus, TradePipeline
from bonds.storage import Database, IngestionRun, Trade

pytestmark = pytest.mark.integration

SOURCE = "faketest"
AS_OF = dt.date(2026, 7, 18)
TRADE_DATE = dt.date(2026, 7, 17)


class FakeTradeSource:
    """A source returning canned trade records (no network)."""

    name = SOURCE

    def __init__(self, *, empty: bool = False) -> None:
        self._empty = empty

    def fetch_trades(self, as_of: dt.date) -> list[TradeRecord]:
        if self._empty:
            return []
        return [
            TradeRecord(
                isin="INE002A07809",
                trade_date=TRADE_DATE,
                source=SOURCE,
                segment="otctrades_listed",
                ltp=99.88,
                lty=7.52,
                no_of_trades=4,
                trade_value=23800.0,
            )
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
            s.execute(delete(Trade).where(Trade.source == SOURCE))
            s.execute(delete(IngestionRun).where(IngestionRun.source == SOURCE))

    wipe()
    yield
    wipe()


def test_run_persists_trades(database: Database) -> None:
    result = TradePipeline(database, source=FakeTradeSource()).run(AS_OF)
    assert result.status is RunStatus.SUCCESS
    assert result.rows == 1
    with database.session() as s:
        trade = s.execute(select(Trade).where(Trade.source == SOURCE)).scalar_one()
    assert trade.isin == "INE002A07809"
    assert trade.trade_date == TRADE_DATE  # session date, not as_of


def test_empty_session_is_success_with_zero_rows(database: Database) -> None:
    result = TradePipeline(database, source=FakeTradeSource(empty=True)).run(AS_OF)
    assert result.status is RunStatus.SUCCESS
    assert result.rows == 0


def test_run_is_idempotent(database: Database) -> None:
    pipeline = TradePipeline(database, source=FakeTradeSource())
    pipeline.run(AS_OF)
    pipeline.run(AS_OF)
    with database.session() as s:
        count = s.execute(
            select(func.count()).select_from(Trade).where(Trade.source == SOURCE)
        ).scalar_one()
    assert count == 1
