"""Yield-curve lookup at an arbitrary maturity.

Every RBI curve-plus-spread valuation rule reads "the central government yield **at equivalent
maturity**", but a bond's residual maturity is almost never one of the published tenor points.
FBIL publishes a quarter-year grid (0.25y to 50y for the G-Sec curves, 0.25y to 14y for the SDL
ZCYC), so the missing values are between two known neighbours and linear interpolation on that
spacing is well inside the precision the marks themselves carry.

Reads only current rows (``superseded_at IS NULL``). For "what did we believe on date X",
include superseded rows explicitly — a restated curve is history, not an error.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left
from typing import Final

from sqlalchemy import Connection, text

GSEC_PAR: Final = "gsec_par"
GSEC_ZCYC: Final = "gsec_zcyc"
SDL_ZCYC: Final = "sdl_zcyc"


class CurveUnavailable(LookupError):  # noqa: N818 — matches DataUnavailable, a control signal
    """No published curve for the requested (curve, date) — a holiday, or before the series."""


class Curve:
    """One published curve for one date, queryable at any maturity."""

    __slots__ = ("_curve", "_quote_date", "_tenors", "_yields")

    def __init__(self, curve: str, quote_date: dt.date, points: list[tuple[float, float]]) -> None:
        if not points:
            raise CurveUnavailable(f"no {curve} points for {quote_date}")
        ordered = sorted(points)
        self._curve = curve
        self._quote_date = quote_date
        self._tenors = [t for t, _ in ordered]
        self._yields = [y for _, y in ordered]

    @property
    def curve(self) -> str:
        """Canonical curve key (e.g. ``gsec_par``)."""
        return self._curve

    @property
    def quote_date(self) -> dt.date:
        """The business date this curve was published for."""
        return self._quote_date

    @property
    def max_tenor(self) -> float:
        """Longest published tenor, in years."""
        return self._tenors[-1]

    def yield_at(self, tenor_years: float) -> float:
        """Yield (percent) at ``tenor_years``, linearly interpolated between grid points.

        Outside the published range the nearest endpoint is returned rather than extrapolated:
        a 60-year residual maturity against a curve that stops at 50 is a data question, not an
        invitation to invent a yield beyond the longest traded point.
        """
        tenors, yields = self._tenors, self._yields
        if tenor_years <= tenors[0]:
            return yields[0]
        if tenor_years >= tenors[-1]:
            return yields[-1]
        i = bisect_left(tenors, tenor_years)
        if tenors[i] == tenor_years:
            return yields[i]
        lo_t, hi_t = tenors[i - 1], tenors[i]
        lo_y, hi_y = yields[i - 1], yields[i]
        weight = (tenor_years - lo_t) / (hi_t - lo_t)
        return lo_y + weight * (hi_y - lo_y)


def load_curve(
    conn: Connection, *, curve: str, quote_date: dt.date, annualized: bool = False
) -> Curve:
    """Load one published curve for one date.

    Args:
        conn: Open connection.
        curve: Curve key — ``gsec_par``, ``gsec_zcyc`` or ``sdl_zcyc``.
        quote_date: Business date.
        annualized: Read ``ytm_annualized`` instead of the semi-annual convention. FBIL publishes
            both; the Directions do not name one, so the caller picks and states which.

    Raises:
        CurveUnavailable: If nothing is published for that curve and date.
    """
    column = "ytm_annualized" if annualized else "ytm_semi_annual"
    rows = conn.execute(
        text(
            f"""
            SELECT tenor_years, {column} AS y
              FROM yield_curves
             WHERE curve = :curve AND quote_date = :quote_date
               AND superseded_at IS NULL AND {column} IS NOT NULL
            """
        ),
        {"curve": curve, "quote_date": quote_date},
    ).all()
    return Curve(curve, quote_date, [(float(r.tenor_years), float(r.y)) for r in rows])


def latest_curve_date(conn: Connection, *, curve: str, on_or_before: dt.date) -> dt.date | None:
    """Newest published date for ``curve`` at or before ``on_or_before``.

    Valuation on a holiday uses the last published curve; this is how the caller finds it
    without silently valuing against a stale curve it did not choose.
    """
    return conn.execute(
        text(
            """
            SELECT max(quote_date) FROM yield_curves
             WHERE curve = :curve AND quote_date <= :on_or_before AND superseded_at IS NULL
            """
        ),
        {"curve": curve, "on_or_before": on_or_before},
    ).scalar()
