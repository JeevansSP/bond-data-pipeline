"""Liquidity pipeline: per-ISIN traded-liquidity metrics into ``security_liquidity``.

Recomputes, for one as-of date, how much each instrument actually traded — last print, prints
and distinct trading days over 1/3/12 months, turnover, and two regulatory verdicts:

* ``traded_within_15d`` — clause 78(1)(i)(c), the traded-price cap on corporate valuations.
* ``active_market`` — the fair-value-hierarchy test (clause 4(1)), which decides Level 1/2/3
  and therefore whether unrealised gains are deducted from CET1.

Two trade sources, deliberately kept separate rather than unioned upstream: ``corporate_trades``
is trade-level (one row per print, so ``prints`` is a true count and turnover is in ₹ lakh), and
``trades`` is a per-session summary (one row per ISIN-day, so ``prints`` comes from the reported
``no_of_trades`` and turnover is on each source's own basis). Recomputed rather than
incrementally updated: the metrics are windowed, so yesterday's row is not a valid starting
point for today's.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from bonds.logging import get_logger
from bonds.pipelines.base import PipelineResult, execute_run
from bonds.storage import Database
from bonds.valuation import TRADED_PRICE_CAP_DAYS

logger = get_logger(__name__)

SOURCE: Final = "derived"
DATASET: Final = "derived.liquidity"

ACTIVE_MARKET_MIN_DAYS_TRADED_3M: Final = 15
"""Distinct trading days in the last 3 months for a market to count as active.

**Not a figure from the Direction text.** Clause 4(1) defines an active market qualitatively —
transactions "with sufficient frequency and volume to provide pricing information on an ongoing
basis" — and names no threshold, which is deliberate: it is an accounting judgement the bank and
its auditor make per instrument. 15 days (roughly one print a week over the quarter) is a
defensible starting point and, measured against our own tape, separates the ~1,800 ISINs that
trade regularly from the ~3,200 that print a handful of times a year. Any client using this
column must set their own threshold and be able to justify it; expose it, never bury it.
"""

ACTIVE_MARKET_MIN_PRINTS_3M: Final = 30
"""Companion volume condition — frequency alone can be met by 15 single-lot prints."""


def _window_params(as_of: dt.date) -> dict[str, object]:
    """Bind values shared by both inserts: the as-of date, its window edges and the thresholds.

    The window edges are computed here rather than as date arithmetic inside a dozen FILTER
    clauses — one statement of "what 3 months means" instead of six.
    """
    return {
        "as_of": as_of,
        "as_of_date": as_of,
        "d30": as_of - dt.timedelta(days=30),
        "d91": as_of - dt.timedelta(days=91),
        "d366": as_of - dt.timedelta(days=366),
        "cap_days": TRADED_PRICE_CAP_DAYS,
        "min_days": ACTIVE_MARKET_MIN_DAYS_TRADED_3M,
        "min_prints": ACTIVE_MARKET_MIN_PRINTS_3M,
    }


class LiquidityPipeline:
    """Recompute ``security_liquidity`` for one as-of date."""

    dataset: Final = DATASET

    def __init__(self, database: Database) -> None:
        self._db = database

    def run(self, as_of: dt.date) -> PipelineResult:
        """Recompute every ISIN's liquidity metrics as at ``as_of`` (idempotent)."""

        def work(session: Session) -> int:
            session.execute(
                text("DELETE FROM security_liquidity WHERE as_of_date = :as_of"),
                {"as_of": as_of},
            )
            corporate = self._insert_corporate(session, as_of)
            sovereign = self._insert_sovereign(session, as_of)
            logger.info(
                "liquidity.computed",
                as_of=as_of.isoformat(),
                corporate=corporate,
                sovereign=sovereign,
            )
            return corporate + sovereign

        return execute_run(self._db, source=SOURCE, dataset=DATASET, run_date=as_of, work=work)

    def _insert_corporate(self, session: Session, as_of: dt.date) -> int:
        """Metrics for the trade-level corporate tape (NSE + BSE)."""
        result = cast(
            "CursorResult[Any]",
            session.execute(
                text(
                    """
                INSERT INTO security_liquidity (
                    isin, as_of_date, instrument_type, last_trade_date, days_since_trade,
                    last_price, last_yield,
                    prints_1m, prints_3m, prints_12m,
                    days_traded_1m, days_traded_3m, days_traded_12m,
                    turnover_1m, turnover_3m, turnover_12m,
                    traded_within_15d, active_market
                )
                WITH windowed AS (
                    SELECT
                        isin,
                        max(trade_date) AS last_trade_date,
                        count(*) FILTER (WHERE trade_date > :d30)  AS prints_1m,
                        count(*) FILTER (WHERE trade_date > :d91)  AS prints_3m,
                        count(*)                                          AS prints_12m,
                        count(DISTINCT trade_date) FILTER (WHERE trade_date > :d30)
                            AS days_traded_1m,
                        count(DISTINCT trade_date) FILTER (WHERE trade_date > :d91)
                            AS days_traded_3m,
                        count(DISTINCT trade_date)                        AS days_traded_12m,
                        sum(trade_value_lakh) FILTER (WHERE trade_date > :d30)
                            AS turnover_1m,
                        sum(trade_value_lakh) FILTER (WHERE trade_date > :d91)
                            AS turnover_3m,
                        sum(trade_value_lakh)                             AS turnover_12m
                      FROM corporate_trades
                     WHERE trade_date <= :as_of AND trade_date > :d366
                     GROUP BY isin
                ),
                -- The last print itself, for the 15-day cap: the *latest* trade on the latest
                -- day, so a client applying clause 78(1)(i)(c) has the rate that print carried.
                last_print AS (
                    SELECT DISTINCT ON (ct.isin) ct.isin, ct.price, ct.trade_yield
                      FROM corporate_trades ct
                      JOIN windowed w ON w.isin = ct.isin AND w.last_trade_date = ct.trade_date
                     ORDER BY ct.isin, ct.trade_date DESC, ct.trade_time DESC NULLS LAST
                )
                SELECT
                    w.isin, :as_of, 'CORP', w.last_trade_date,
                    (:as_of_date - w.last_trade_date),
                    p.price, p.trade_yield,
                    w.prints_1m, w.prints_3m, w.prints_12m,
                    w.days_traded_1m, w.days_traded_3m, w.days_traded_12m,
                    w.turnover_1m, w.turnover_3m, w.turnover_12m,
                    (:as_of_date - w.last_trade_date) <= :cap_days,
                    w.days_traded_3m >= :min_days AND w.prints_3m >= :min_prints
                  FROM windowed w
                  LEFT JOIN last_print p ON p.isin = w.isin
                """
                ),
                {
                    **_window_params(as_of),
                },
            ),
        )
        return result.rowcount

    def _insert_sovereign(self, session: Session, as_of: dt.date) -> int:
        """Metrics for the per-session sovereign tape (CCIL) and the NSE CBM daily archive.

        ``no_of_trades`` is the session's reported print count, so it stands in for ``prints``;
        turnover is each source's own basis (CCIL reports face value traded, NSE session
        turnover) and must not be summed across sources — hence one row per ISIN, taking the
        most active source, rather than a union.
        """
        result = cast(
            "CursorResult[Any]",
            session.execute(
                text(
                    """
                INSERT INTO security_liquidity (
                    isin, as_of_date, instrument_type, last_trade_date, days_since_trade,
                    last_price, last_yield,
                    prints_1m, prints_3m, prints_12m,
                    days_traded_1m, days_traded_3m, days_traded_12m,
                    turnover_1m, turnover_3m, turnover_12m,
                    traded_within_15d, active_market
                )
                WITH t AS (
                    SELECT tr.isin, tr.trade_date, tr.source,
                           coalesce(tr.no_of_trades, 1) AS n_trades,
                           tr.trade_value, tr.ltp, tr.lty
                      FROM trades tr
                     WHERE tr.trade_date <= :as_of AND tr.trade_date > :d366
                       -- Exclude ISINs covered by the trade-level corporate tape, so a
                       -- corporate bond is never described twice on two different turnover
                       -- bases. Tested against corporate_trades rather than against the rows
                       -- _insert_corporate just wrote: reading the table being written works
                       -- only while that insert happens to run first, and swapping the two
                       -- calls would silently reintroduce the duplicates with no error.
                       AND NOT EXISTS (
                             SELECT 1 FROM corporate_trades ct
                              WHERE ct.isin = tr.isin
                                AND ct.trade_date <= :as_of AND ct.trade_date > :d366
                           )
                ),
                windowed AS (
                    SELECT
                        isin,
                        max(trade_date) AS last_trade_date,
                        coalesce(
                            sum(n_trades) FILTER (WHERE trade_date > :d30), 0
                        ) AS prints_1m,
                        coalesce(
                            sum(n_trades) FILTER (WHERE trade_date > :d91), 0
                        ) AS prints_3m,
                        coalesce(sum(n_trades), 0) AS prints_12m,
                        count(DISTINCT trade_date) FILTER (WHERE trade_date > :d30)
                            AS days_traded_1m,
                        count(DISTINCT trade_date) FILTER (WHERE trade_date > :d91)
                            AS days_traded_3m,
                        count(DISTINCT trade_date)                             AS days_traded_12m,
                        sum(trade_value) FILTER (WHERE trade_date > :d30) AS turnover_1m,
                        sum(trade_value) FILTER (WHERE trade_date > :d91) AS turnover_3m,
                        sum(trade_value)                                       AS turnover_12m
                      FROM t GROUP BY isin
                ),
                last_session AS (
                    SELECT DISTINCT ON (t.isin) t.isin, t.ltp, t.lty
                      FROM t JOIN windowed w ON w.isin = t.isin AND w.last_trade_date = t.trade_date
                     ORDER BY t.isin, t.trade_date DESC, t.n_trades DESC
                )
                SELECT
                    w.isin, :as_of,
                    coalesce(s.instrument_type, 'UNKNOWN'),
                    w.last_trade_date, (:as_of_date - w.last_trade_date),
                    l.ltp, l.lty,
                    w.prints_1m, w.prints_3m, w.prints_12m,
                    w.days_traded_1m, w.days_traded_3m, w.days_traded_12m,
                    w.turnover_1m, w.turnover_3m, w.turnover_12m,
                    (:as_of_date - w.last_trade_date) <= :cap_days,
                    w.days_traded_3m >= :min_days AND w.prints_3m >= :min_prints
                  FROM windowed w
                  LEFT JOIN last_session l ON l.isin = w.isin
                  LEFT JOIN securities s ON s.isin = w.isin
                """
                ),
                {
                    **_window_params(as_of),
                },
            ),
        )
        return result.rowcount
