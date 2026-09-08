"""Tests for yield-curve lookup at an arbitrary maturity."""

from __future__ import annotations

import datetime as dt

import pytest

from bonds.curves import Curve, CurveUnavailable

DATE = dt.date(2026, 9, 4)
# A quarter-year grid, as FBIL publishes.
POINTS = [(0.25, 5.14), (0.5, 5.40), (0.75, 5.60), (1.0, 5.82), (4.25, 6.47), (4.5, 6.53)]


def _curve() -> Curve:
    return Curve("gsec_par", DATE, POINTS)


def test_exact_grid_point_is_returned_unchanged() -> None:
    assert _curve().yield_at(1.0) == pytest.approx(5.82)


def test_between_grid_points_interpolates_linearly() -> None:
    # 4.37y sits 48% of the way from 4.25 to 4.5, so the yield sits 48% of the way from 6.47.
    assert _curve().yield_at(4.37) == pytest.approx(6.47 + 0.48 * (6.53 - 6.47), abs=1e-3)


def test_outside_the_published_range_clamps_rather_than_extrapolates() -> None:
    # A residual maturity beyond the longest published tenor is a data question, not an
    # invitation to invent a yield past the longest traded point.
    assert _curve().yield_at(0.01) == pytest.approx(5.14)
    assert _curve().yield_at(60.0) == pytest.approx(6.53)


def test_unordered_points_are_sorted() -> None:
    shuffled = Curve("gsec_par", DATE, list(reversed(POINTS)))
    assert shuffled.yield_at(1.0) == pytest.approx(5.82)
    assert shuffled.max_tenor == pytest.approx(4.5)


def test_empty_curve_is_unavailable() -> None:
    # A curve with no points is a holiday or a pre-series date, not a curve of zeros.
    with pytest.raises(CurveUnavailable):
        Curve("gsec_par", DATE, [])
