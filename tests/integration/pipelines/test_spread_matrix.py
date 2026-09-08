"""Integration tests for the spread-matrix pipeline (needs Postgres)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select, text

from bonds.pipelines.base import RunStatus
from bonds.pipelines.spread_matrix import MIN_TRADES_PER_CELL, SpreadMatrixPipeline
from bonds.storage import Database
from bonds.storage.schema import (
    CorporateSpreadPoint,
    CorporateTrade,
    Security,
    SecurityAttributeHistory,
    YieldCurve,
)

pytestmark = pytest.mark.integration

# Far outside the published range: the fixture writes a gsec_par curve for this date, so a
# date the real feed also covers would have the cleanup delete published rows.
QUOTE_DATE = dt.date(1995, 6, 15)
CURVE = "gsec_par"
RATED = "INSPR0000001"  # AAA, 5y, plenty of prints
THIN = "INSPR0000002"  # AA, 5y, below the per-cell floor
UNRATED = "INSPR0000003"  # no rating row -> excluded entirely
ISINS = (RATED, THIN, UNRATED)


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database()
    database.create_all()

    def _clean() -> None:
        with database.session() as s:
            s.execute(delete(CorporateTrade).where(CorporateTrade.isin.in_(ISINS)))
            s.execute(
                delete(SecurityAttributeHistory).where(SecurityAttributeHistory.isin.in_(ISINS))
            )
            s.execute(delete(Security).where(Security.isin.in_(ISINS)))
            s.execute(
                delete(CorporateSpreadPoint).where(CorporateSpreadPoint.quote_date == QUOTE_DATE)
            )
            s.execute(
                delete(YieldCurve).where(
                    YieldCurve.curve == CURVE, YieldCurve.quote_date == QUOTE_DATE
                )
            )

    _clean()
    yield database
    _clean()


def _seed(db: Database, *, with_curve: bool = True) -> None:
    with db.session() as s:
        if with_curve:
            for tenor in (0.25, 5.0, 50.0):
                s.execute(
                    text(
                        "INSERT INTO yield_curves (curve, quote_date, tenor_years, source,"
                        " ytm_semi_annual) VALUES (:c, :d, :t, 'fbil', 7.0)"
                    ),
                    {"c": CURVE, "d": QUOTE_DATE, "t": tenor},
                )
        maturity = QUOTE_DATE + dt.timedelta(days=365 * 5)
        for isin, rating, prints, yield_pct in (
            (RATED, "CRISIL AAA", 10, 8.0),
            (THIN, "CRISIL AA", MIN_TRADES_PER_CELL - 1, 9.0),
            (UNRATED, None, 10, 9.5),
        ):
            s.execute(
                text(
                    "INSERT INTO securities (isin, instrument_type, source, maturity_date,"
                    " first_seen, last_seen) VALUES (:i, 'CORP', 'test', :m, :d, :d)"
                ),
                {"i": isin, "m": maturity, "d": QUOTE_DATE},
            )
            if rating:
                s.execute(
                    text(
                        "INSERT INTO security_attribute_history (isin, attribute, value,"
                        " valid_from, source) VALUES (:i, 'credit_rating', :v, :d, 'test')"
                    ),
                    {"i": isin, "v": rating, "d": QUOTE_DATE - dt.timedelta(days=30)},
                )
            for n in range(prints):
                day = QUOTE_DATE - dt.timedelta(days=n)
                s.execute(
                    text(
                        "INSERT INTO corporate_trades (isin, trade_date, source, trade_time,"
                        " price, trade_yield, trade_value_lakh)"
                        " VALUES (:i, :d, 'nse', :t, 100, :y, 10)"
                    ),
                    {
                        "i": isin,
                        "d": day,
                        "t": dt.datetime.combine(day, dt.time(10)),
                        "y": yield_pct,
                    },
                )


def _cells(db: Database) -> dict[tuple[str, float], CorporateSpreadPoint]:
    with db.session() as s:
        rows = s.execute(
            select(CorporateSpreadPoint).where(CorporateSpreadPoint.quote_date == QUOTE_DATE)
        ).scalars()
        return {(r.rating, r.tenor_bucket): r for r in rows}


def test_spread_is_the_traded_yield_over_the_cg_curve(db: Database) -> None:
    _seed(db)
    assert SpreadMatrixPipeline(db).run(QUOTE_DATE).status is RunStatus.SUCCESS
    cell = _cells(db)[("AAA", 5.0)]
    assert cell.trade_count == 10
    assert cell.isin_count == 1
    assert cell.wavg_yield_pct == pytest.approx(8.0)
    assert cell.cg_yield_pct == pytest.approx(7.0)
    assert cell.spread_bp == pytest.approx(100.0)  # 8.0% - 7.0% = 100bp


def test_cells_below_the_print_floor_are_not_written(db: Database) -> None:
    # A cell computed from one or two prints is an anecdote. An absent cell is honest.
    _seed(db)
    SpreadMatrixPipeline(db).run(QUOTE_DATE)
    assert ("AA", 5.0) not in _cells(db)


def test_unrated_paper_is_excluded(db: Database) -> None:
    # The grid is keyed on rating; a bond with no rating in force on the quote date has no cell.
    _seed(db)
    SpreadMatrixPipeline(db).run(QUOTE_DATE)
    assert all(rating for rating, _ in _cells(db))


def test_rerun_replaces_rather_than_duplicates(db: Database) -> None:
    _seed(db)
    SpreadMatrixPipeline(db).run(QUOTE_DATE)
    SpreadMatrixPipeline(db).run(QUOTE_DATE)
    with db.session() as s:
        count = s.execute(
            text(
                "SELECT count(*) FROM corporate_spread_matrix"
                " WHERE quote_date = :d AND rating = 'AAA'"
            ),
            {"d": QUOTE_DATE},
        ).scalar_one()
    assert count == 1


def test_a_date_with_no_published_curve_is_skipped(db: Database) -> None:
    # A spread without a base curve is not a spread. Skipping lets catch-up retry once the
    # curve lands, rather than banking the day with no cells.
    # 1990 predates every published curve, so nothing needs deleting to reach this branch.
    # An earlier version of this test deleted `quote_date >= QUOTE_DATE` to force it and took
    # seven weeks of real curve data with it: an integration test must never remove rows it did
    # not create.
    _seed(db, with_curve=False)
    assert SpreadMatrixPipeline(db).run(dt.date(1990, 1, 1)).status is RunStatus.SKIPPED
