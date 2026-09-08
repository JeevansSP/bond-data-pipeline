"""Integration tests for the liquidity pipeline (needs Postgres)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select, text

from bonds.pipelines.base import RunStatus
from bonds.pipelines.liquidity import LiquidityPipeline
from bonds.storage import Database
from bonds.storage.schema import CorporateTrade, SecurityLiquidity, Trade

pytestmark = pytest.mark.integration

AS_OF = dt.date(2026, 7, 17)
ISIN_LIQUID = "INLIQ0000001"
ISIN_STALE = "INLIQ0000002"
ISIN_SOVEREIGN = "INLIQ0000003"
ISIN_BOTH = "INLIQ0000004"
ISINS = (ISIN_LIQUID, ISIN_STALE, ISIN_SOVEREIGN, ISIN_BOTH)


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database()
    database.create_all()

    def _clean() -> None:
        with database.session() as s:
            s.execute(delete(CorporateTrade).where(CorporateTrade.isin.in_(ISINS)))
            s.execute(delete(Trade).where(Trade.isin.in_(ISINS)))
            s.execute(delete(SecurityLiquidity).where(SecurityLiquidity.isin.in_(ISINS)))

    _clean()
    yield database
    _clean()


def _seed(db: Database) -> None:
    """One instrument printing most days, one whose last print is a year old."""
    with db.session() as s:
        for offset in range(0, 80):
            day = AS_OF - dt.timedelta(days=offset)
            s.execute(
                text(
                    "INSERT INTO corporate_trades (isin, trade_date, source, trade_time, price,"
                    " trade_yield, trade_value_lakh) VALUES (:i, :d, 'nse', :t, 100, 7.5, 25)"
                ),
                {"i": ISIN_LIQUID, "d": day, "t": dt.datetime.combine(day, dt.time(10, 0))},
            )
        old = AS_OF - dt.timedelta(days=300)
        s.execute(
            text(
                "INSERT INTO corporate_trades (isin, trade_date, source, trade_time, price,"
                " trade_yield, trade_value_lakh) VALUES (:i, :d, 'nse', :t, 98, 9.0, 5)"
            ),
            {"i": ISIN_STALE, "d": old, "t": dt.datetime.combine(old, dt.time(10, 0))},
        )


def _row(db: Database, isin: str) -> SecurityLiquidity:
    with db.session() as s:
        return s.execute(
            select(SecurityLiquidity).where(
                SecurityLiquidity.isin == isin, SecurityLiquidity.as_of_date == AS_OF
            )
        ).scalar_one()


def test_liquid_instrument_is_within_15_days_and_active(db: Database) -> None:
    _seed(db)
    assert LiquidityPipeline(db).run(AS_OF).status is RunStatus.SUCCESS
    row = _row(db, ISIN_LIQUID)
    assert row.days_since_trade == 0
    assert row.traded_within_15d
    assert row.active_market
    assert row.days_traded_1m == 30
    assert row.prints_3m == 80
    assert row.turnover_3m == pytest.approx(80 * 25)
    assert row.last_price == pytest.approx(100.0)


def test_stale_instrument_fails_both_verdicts(db: Database) -> None:
    _seed(db)
    LiquidityPipeline(db).run(AS_OF)
    row = _row(db, ISIN_STALE)
    assert row.days_since_trade == 300
    assert not row.traded_within_15d
    assert not row.active_market
    assert row.prints_3m == 0


def test_windows_are_nested(db: Database) -> None:
    _seed(db)
    LiquidityPipeline(db).run(AS_OF)
    row = _row(db, ISIN_LIQUID)
    assert row.prints_1m <= row.prints_3m <= row.prints_12m
    assert row.days_traded_1m <= row.days_traded_3m <= row.days_traded_12m


def test_rerun_replaces_rather_than_duplicates(db: Database) -> None:
    _seed(db)
    LiquidityPipeline(db).run(AS_OF)
    LiquidityPipeline(db).run(AS_OF)
    with db.session() as s:
        count = s.execute(
            text("SELECT count(*) FROM security_liquidity WHERE isin = :i AND as_of_date = :d"),
            {"i": ISIN_LIQUID, "d": AS_OF},
        ).scalar_one()
    assert count == 1


def test_metrics_are_point_in_time(db: Database) -> None:
    # The whole reason this is a table and not a view: an as-of a month earlier must see only
    # the prints that existed by then.
    _seed(db)
    earlier = AS_OF - dt.timedelta(days=40)
    LiquidityPipeline(db).run(earlier)
    with db.session() as s:
        row = s.execute(
            select(SecurityLiquidity).where(
                SecurityLiquidity.isin == ISIN_LIQUID, SecurityLiquidity.as_of_date == earlier
            )
        ).scalar_one()
    assert row.last_trade_date == earlier
    assert row.prints_12m == 80 - 40
    with db.session() as s:
        s.execute(delete(SecurityLiquidity).where(SecurityLiquidity.as_of_date == earlier))


def _seed_sovereign(db: Database) -> None:
    """A CCIL-style per-session tape: one row per ISIN-day carrying a reported print count."""
    with db.session() as s:
        for offset in range(0, 60):
            day = AS_OF - dt.timedelta(days=offset)
            for isin in (ISIN_SOVEREIGN, ISIN_BOTH):
                s.execute(
                    text(
                        "INSERT INTO trades (isin, trade_date, source, segment, ltp, lty,"
                        " no_of_trades, trade_value) VALUES (:i, :d, 'ccil', 'GSEC', 101, 6.9,"
                        " 4, 500)"
                    ),
                    {"i": isin, "d": day},
                )
        # ISIN_BOTH also has trade-level corporate prints, so it must be described once, on the
        # corporate basis — not twice on two different turnover bases.
        for offset in range(0, 40):
            day = AS_OF - dt.timedelta(days=offset)
            s.execute(
                text(
                    "INSERT INTO corporate_trades (isin, trade_date, source, trade_time, price,"
                    " trade_yield, trade_value_lakh) VALUES (:i, :d, 'nse', :t, 100, 7.5, 12)"
                ),
                {"i": ISIN_BOTH, "d": day, "t": dt.datetime.combine(day, dt.time(10, 0))},
            )


def test_sovereign_tape_uses_reported_print_counts(db: Database) -> None:
    # The per-session tape has one row per ISIN-day, so ``prints`` comes from the reported
    # no_of_trades rather than from a row count.
    _seed_sovereign(db)
    LiquidityPipeline(db).run(AS_OF)
    row = _row(db, ISIN_SOVEREIGN)
    assert row.days_traded_1m == 30
    assert row.prints_1m == 30 * 4  # 4 reported trades per session
    assert row.traded_within_15d
    assert row.last_price == pytest.approx(101.0)


def test_an_isin_on_both_tapes_is_described_once(db: Database) -> None:
    # Otherwise the same bond appears twice on incompatible turnover bases (₹ lakh notional vs
    # face value traded). The dedup must not depend on which insert happens to run first.
    _seed_sovereign(db)
    LiquidityPipeline(db).run(AS_OF)
    with db.session() as s:
        rows = (
            s.execute(
                select(SecurityLiquidity).where(
                    SecurityLiquidity.isin == ISIN_BOTH, SecurityLiquidity.as_of_date == AS_OF
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    # 40 daily prints of 12 lakh each -> the corporate basis, not the sovereign 500-per-session.
    assert rows[0].turnover_1m == pytest.approx(30 * 12)


def test_sovereign_rows_with_no_securities_entry_are_typed_unknown(db: Database) -> None:
    # The trade tapes reach back further than any reference source, so some ISINs have no
    # securities row. They are still real trades and must not be dropped.
    _seed_sovereign(db)
    LiquidityPipeline(db).run(AS_OF)
    assert _row(db, ISIN_SOVEREIGN).instrument_type == "UNKNOWN"
