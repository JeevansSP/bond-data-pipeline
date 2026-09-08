"""Tests for the RBI curve-plus-spread valuation engine."""

from __future__ import annotations

import datetime as dt

import pytest

from bonds.curves import Curve
from bonds.sovereign import SovereignClass
from bonds.valuation import (
    DEFAULT_RATING_SPREAD_BP,
    MIN_CORPORATE_SPREAD_BP,
    normalize_rating,
    price_corporate,
    price_sovereign,
    residual_maturity_years,
)

AS_OF = dt.date(2026, 9, 4)
# Flat curve at 7% so every assertion is about the spread, not the interpolation.
FLAT = Curve("gsec_par", AS_OF, [(0.25, 7.0), (50.0, 7.0)])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AAA", "AAA"),
        ("AA", "AA"),
        # The +/- notch must survive the agency prefix and the outlook/structure suffixes: a
        # trailing \b after the modifier fails on "AA+" (both it and the next character are
        # non-word), which silently read every AA+ as AA.
        ("CRISIL AA+", "AA+"),
        ("[ICRA]AA-(CE)", "AA-"),
        ("IND AA+/Stable", "AA+"),
        ("PP-MLD A+", "A+"),
        ("BB+(CE)", "BB+"),
        ("BB/Stable", "BB"),
        ("ACUITE BBB-", "BBB-"),
        ("CARE A", "A"),
        ("C+", "C+"),
        ("D", "D"),
        ("PP-MLD  D", "D"),
        # Status markers are the absence of a rating, not a low one. Without the leading word
        # boundary the bare "A" alternative matched the A inside WITHDRAWN and valued 143
        # withdrawn-rating ISINs as single-A paper.
        ("WITHDRAWN", None),
        ("SUSPENDED", None),
        ("0", None),
        (None, None),
    ],
)
def test_normalize_rating(raw: str | None, expected: str | None) -> None:
    assert normalize_rating(raw) == expected


def test_every_notch_in_the_grid_round_trips() -> None:
    for notch in DEFAULT_RATING_SPREAD_BP:
        assert normalize_rating(notch) == notch


def test_grid_widens_monotonically_down_the_scale() -> None:
    spreads = list(DEFAULT_RATING_SPREAD_BP.values())
    assert spreads == sorted(spreads)
    assert min(spreads) >= MIN_CORPORATE_SPREAD_BP


def test_residual_maturity_floors_at_zero() -> None:
    assert residual_maturity_years(dt.date(2025, 1, 1), AS_OF) == 0.0
    assert residual_maturity_years(AS_OF + dt.timedelta(days=365), AS_OF) == pytest.approx(1.0)


def test_corporate_valuation_is_curve_plus_rating_spread() -> None:
    v = price_corporate(
        isin="INE013A07317",
        as_of=AS_OF,
        maturity_date=AS_OF + dt.timedelta(days=1825),
        rating="CRISIL AAA",
        cg_curve=FLAT,
    )
    assert v.base_curve_yield_pct == pytest.approx(7.0)
    assert v.spread_bp == pytest.approx(50.0)
    assert v.yield_pct == pytest.approx(7.5)
    assert v.rule == "corporate:AAA:CG+50bp"


def test_unrated_is_never_valued_above_investment_grade() -> None:
    # Clause 78(1)(i)(b). An unrated bond must yield at least as much as (i.e. be worth no more
    # than) a rated bond of equivalent maturity.
    unrated = price_corporate(
        isin="INE013A07317",
        as_of=AS_OF,
        maturity_date=AS_OF + dt.timedelta(days=1825),
        rating=None,
        cg_curve=FLAT,
    )
    worst_ig = price_corporate(
        isin="INE013A07317",
        as_of=AS_OF,
        maturity_date=AS_OF + dt.timedelta(days=1825),
        rating="BBB-",
        cg_curve=FLAT,
    )
    assert unrated.yield_pct >= worst_ig.yield_pct


def test_withdrawn_rating_is_treated_as_unrated() -> None:
    withdrawn = price_corporate(
        isin="INE013A07317",
        as_of=AS_OF,
        maturity_date=AS_OF + dt.timedelta(days=1825),
        rating="WITHDRAWN",
        cg_curve=FLAT,
    )
    assert withdrawn.rule.startswith("corporate:UNRATED")


def test_a_wider_traded_yield_caps_the_valuation() -> None:
    # Clause 78(1)(i)(c): a print within 15 days caps *value*, i.e. floors *yield*.
    v = price_corporate(
        isin="INE013A07317",
        as_of=AS_OF,
        maturity_date=AS_OF + dt.timedelta(days=1825),
        rating="AAA",
        cg_curve=FLAT,
        traded_yield_pct=9.5,
    )
    assert v.capped_by_trade
    assert v.yield_pct == pytest.approx(9.5)


def test_a_tighter_traded_yield_does_not_raise_the_valuation() -> None:
    # The clause is a cap on value, not a mark-to-trade: a print that would make the bond worth
    # *more* than the grid says does not override the grid.
    v = price_corporate(
        isin="INE013A07317",
        as_of=AS_OF,
        maturity_date=AS_OF + dt.timedelta(days=1825),
        rating="AAA",
        cg_curve=FLAT,
        traded_yield_pct=6.0,
    )
    assert not v.capped_by_trade
    assert v.yield_pct == pytest.approx(7.5)


@pytest.mark.parametrize(
    ("sovereign_class", "expected_spread"),
    [
        (SovereignClass.SPECIAL_GOI, 25.0),
        (SovereignClass.STATE_SPECIAL, 50.0),
        (SovereignClass.DISCOM, 100.0),
    ],
)
def test_special_sovereign_takes_its_class_spread(
    sovereign_class: SovereignClass, expected_spread: float
) -> None:
    v = price_sovereign(
        isin="IN0020210012",
        as_of=AS_OF,
        maturity_date=AS_OF + dt.timedelta(days=1825),
        sovereign_class=sovereign_class,
        cg_curve=FLAT,
    )
    assert v is not None
    assert v.spread_bp == pytest.approx(expected_spread)
    assert v.yield_pct == pytest.approx(7.0 + expected_spread / 100.0)


@pytest.mark.parametrize(
    "sovereign_class", [SovereignClass.PLAIN, SovereignClass.UDAY, SovereignClass.FRB]
)
def test_fbil_priced_classes_return_none(sovereign_class: SovereignClass) -> None:
    # These are a lookup in ``valuations``, not a calculation. Returning a computed figure would
    # invite using the wrong one.
    assert (
        price_sovereign(
            isin="IN0020210012",
            as_of=AS_OF,
            maturity_date=AS_OF + dt.timedelta(days=1825),
            sovereign_class=sovereign_class,
            cg_curve=FLAT,
        )
        is None
    )


def test_explicit_spread_overrides_the_class_default() -> None:
    # A holder who knows a DCMB is state-guaranteed applies 75bp (78(2)(ii)) instead of 100.
    v = price_sovereign(
        isin="IN0020210012",
        as_of=AS_OF,
        maturity_date=AS_OF + dt.timedelta(days=1825),
        sovereign_class=SovereignClass.DISCOM,
        cg_curve=FLAT,
        spread_bp=75.0,
    )
    assert v is not None and v.spread_bp == pytest.approx(75.0)
