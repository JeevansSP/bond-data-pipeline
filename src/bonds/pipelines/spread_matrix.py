"""Spread-matrix pipeline: a daily rating x tenor corporate spread grid from actual prints.

FIMMDA publishes the market's reference matrix, and it is the benchmark banks value against —
but its spreads are **poll-derived** down to AA-, and below AA- they are set from a trailing
three-month traded level and then held fixed for three months. That is a reasonable answer to
thin data; it is not a measurement.

This is the same grid measured from ``corporate_trades``: for each (rating notch, tenor bucket),
the notional-weighted yield of the prints in a trailing window, minus the G-Sec par yield at the
bucket's tenor. Where our tape is thick the cell is a measurement; where it is thin the cell
carries its own ``trade_count`` and ``isin_count`` so the caller can see that and fall back.

Tenor buckets match FIMMDA's published grid (0.5, 1-10, 15 years) so the two are directly
comparable cell by cell — the comparison being the product, not the grid.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from bonds.curves import GSEC_PAR, CurveUnavailable, latest_curve_date, load_curve
from bonds.logging import get_logger
from bonds.pipelines.base import PipelineResult, execute_run
from bonds.sources.base import DataUnavailable
from bonds.storage import Database
from bonds.valuation import normalize_rating

logger = get_logger(__name__)

SOURCE: Final = "derived"
DATASET: Final = "derived.spread_matrix"

TENOR_BUCKETS: Final[tuple[float, ...]] = (0.5, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15)
"""FIMMDA's published maturity grid, so our cells line up with theirs."""

DEFAULT_LOOKBACK_DAYS: Final = 30
"""Trailing window of prints per cell.

A single day's prints are far too thin to fill a 21 x 12 grid — the median corporate ISIN trades
a handful of times a *year*. 30 days keeps a cell responsive while giving most investment-grade
buckets enough prints to weight. Widen it for the sub-investment-grade end, where FIMMDA itself
falls back to three months.
"""

MIN_TRADES_PER_CELL: Final = 3
"""Below this a cell is one or two prints wide and the weighted yield is an anecdote. Such cells
are not written at all — an absent cell is honest; a cell computed from one print is not."""


def bucket_for(tenor_years: float) -> float:
    """The published tenor bucket a residual maturity falls in (nearest bucket, not rounded up).

    Nearest rather than next-highest because the grid is uneven at both ends: bucketing a 12-year
    bond up to 15 stretches it three years, while nearest puts it in 10, two years away.
    """
    return min(TENOR_BUCKETS, key=lambda b: abs(b - tenor_years))


class SpreadMatrixPipeline:
    """Recompute ``corporate_spread_matrix`` for one quote date."""

    dataset: Final = DATASET

    def __init__(self, database: Database, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> None:
        self._db = database
        self._lookback_days = lookback_days

    def run(self, quote_date: dt.date) -> PipelineResult:
        """Recompute the grid as at ``quote_date`` (idempotent).

        Raises nothing: a date with no published CG curve records SKIPPED, because a spread
        without a base curve is not a spread.
        """

        def work(session: Session) -> int:
            curve_date = latest_curve_date(
                session.connection(), curve=GSEC_PAR, on_or_before=quote_date
            )
            if curve_date is None:
                raise DataUnavailable(f"no {GSEC_PAR} curve published on or before {quote_date}")
            try:
                curve = load_curve(session.connection(), curve=GSEC_PAR, quote_date=curve_date)
            except CurveUnavailable as exc:
                raise DataUnavailable(str(exc)) from exc

            session.execute(
                text("DELETE FROM corporate_spread_matrix WHERE quote_date = :d"),
                {"d": quote_date},
            )
            cells = self._aggregate_cells(session, quote_date)
            written = 0
            for cell in cells:
                cg_yield = curve.yield_at(cell["tenor_bucket"])
                session.execute(
                    text(
                        """
                        INSERT INTO corporate_spread_matrix (
                            quote_date, rating, tenor_bucket, trade_count, isin_count,
                            notional_lakh, wavg_yield_pct, median_yield_pct, cg_yield_pct,
                            spread_bp, lookback_days
                        ) VALUES (
                            :quote_date, :rating, :tenor_bucket, :trade_count, :isin_count,
                            :notional_lakh, :wavg_yield_pct, :median_yield_pct, :cg_yield_pct,
                            :spread_bp, :lookback_days
                        )
                        """
                    ),
                    {
                        **cell,
                        "quote_date": quote_date,
                        "cg_yield_pct": cg_yield,
                        "spread_bp": (cell["wavg_yield_pct"] - cg_yield) * 100.0,
                        "lookback_days": self._lookback_days,
                    },
                )
                written += 1
            logger.info(
                "spread_matrix.computed",
                quote_date=quote_date.isoformat(),
                curve_date=curve_date.isoformat(),
                cells=written,
            )
            return written

        return execute_run(self._db, source=SOURCE, dataset=DATASET, run_date=quote_date, work=work)

    def _aggregate_cells(self, session: Session, quote_date: dt.date) -> list[dict[str, Any]]:
        """Notional-weighted yield per (rating notch, tenor bucket) over the trailing window.

        The rating is the one in force **on the quote date** (the SCD-2 window), not today's:
        a matrix recomputed for last March must use last March's ratings or it is not a
        historical matrix.
        """
        rows = cast(
            "CursorResult[Any]",
            session.execute(
                text(
                    """
                    WITH priced AS (
                        SELECT
                            ct.isin,
                            ct.trade_yield,
                            ct.trade_value_lakh,
                            (s.maturity_date - CAST(:quote_date AS date)) / 365.0 AS tenor_years,
                            h.value AS raw_rating
                          FROM corporate_trades ct
                          JOIN securities s ON s.isin = ct.isin
                          JOIN security_attribute_history h
                            ON h.isin = ct.isin AND h.attribute = 'credit_rating'
                           AND h.valid_from <= :quote_date
                           AND (h.valid_to IS NULL OR h.valid_to > :quote_date)
                         WHERE ct.trade_date <= :quote_date
                           AND ct.trade_date > CAST(:quote_date AS date) - :lookback
                           AND ct.trade_yield IS NOT NULL
                           AND ct.trade_value_lakh IS NOT NULL
                           AND ct.trade_value_lakh > 0
                           AND s.maturity_date IS NOT NULL
                           AND s.maturity_date > :quote_date
                           -- As-published outliers (see the corp_trade_extreme_yields DQ check)
                           -- would drag a weighted mean without adding information.
                           AND ct.trade_yield BETWEEN 0 AND 40
                    )
                    SELECT isin, trade_yield, trade_value_lakh, tenor_years, raw_rating
                      FROM priced
                    """
                ),
                {
                    "quote_date": quote_date,
                    "lookback": self._lookback_days,
                },
            ),
        ).all()

        # Bucketing and rating normalisation happen in Python so the grid uses exactly the same
        # notch vocabulary as the valuation engine (bonds.valuation.normalize_rating) — two
        # implementations of "what rating is this" is how the two products drift apart.
        cells: dict[tuple[str, float], dict[str, Any]] = {}
        for row in rows:
            notch = normalize_rating(row.raw_rating)
            if notch is None:
                continue
            key = (notch, bucket_for(float(row.tenor_years)))
            cell = cells.setdefault(
                key,
                {
                    "rating": notch,
                    "tenor_bucket": key[1],
                    "_weighted": 0.0,
                    "_notional": 0.0,
                    "_yields": [],
                    "_isins": set(),
                },
            )
            weight = float(row.trade_value_lakh)
            cell["_weighted"] += float(row.trade_yield) * weight
            cell["_notional"] += weight
            cell["_yields"].append(float(row.trade_yield))
            cell["_isins"].add(row.isin)

        out: list[dict[str, Any]] = []
        for cell in cells.values():
            yields = sorted(cell["_yields"])
            if len(yields) < MIN_TRADES_PER_CELL:
                continue
            mid = len(yields) // 2
            median = yields[mid] if len(yields) % 2 else (yields[mid - 1] + yields[mid]) / 2
            out.append(
                {
                    "rating": cell["rating"],
                    "tenor_bucket": cell["tenor_bucket"],
                    "trade_count": len(yields),
                    "isin_count": len(cell["_isins"]),
                    "notional_lakh": cell["_notional"],
                    "wavg_yield_pct": cell["_weighted"] / cell["_notional"],
                    "median_yield_pct": median,
                }
            )
        return out
