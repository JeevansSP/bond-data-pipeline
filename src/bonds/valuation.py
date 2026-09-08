"""The RBI curve-plus-spread valuation rule, as an engine.

What the Directions actually prescribe, for a bank valuing a holding it cannot mark to an
observable price (clause numbers are Commercial Banks, RBI/DOR/2025-26/162):

1. **Quoted** securities take the exchange/platform price (74).
2. **Unquoted central government and SDL** take the published FBIL price/YTM (76).
3. **T-Bills** stay at carrying cost (75) — no valuation.
4. **Corporate bonds** take the central-government yield *at equivalent maturity* plus a
   rating-graded spread of at least 50bp (78(1)(i)(a)), **capped by a traded price from the last
   15 days** where one exists (78(1)(i)(c)).
5. **Special sovereign paper** takes the central-government yield at equivalent maturity plus a
   class-specific spread — see :mod:`bonds.sovereign`.

This module implements 4 and 5, which are the ones that need a curve, a spread and a residual
maturity rather than a lookup. Steps 1-3 are a query, not a calculation.

**The rating-graded spread grid in :data:`DEFAULT_RATING_SPREAD_BP` is not sourced from the
Direction text.** Clause 78(1)(i)(a) is recorded in
``docs/business/2026-07-27_222030_rbi-investment-directions-demand-map.md`` only as "CG
equivalent-maturity + >=50bp, rating-graded"; the per-rating figures were never extracted. The
defaults here honour the 50bp floor and step monotonically down the rating scale, which is the
right *shape*, but they are placeholders: **read the clause and replace them before any output
of this engine reaches a client**. Pass your own grid to :func:`price_corporate` meanwhile.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Final

from bonds.curves import Curve
from bonds.sovereign import RBI_SPREAD_BP, SovereignClass

MIN_CORPORATE_SPREAD_BP: Final = 50.0
"""Clause 78(1)(i)(a): the spread over the CG curve is "not less than 50 basis points"."""

TRADED_PRICE_CAP_DAYS: Final = 15
"""Clause 78(1)(i)(c): a traded price from within 15 days caps the computed valuation."""

# PLACEHOLDER — see the module docstring. Monotonic down the scale, floored at the 50bp minimum.
# The scale runs to D rather than stopping at BBB-: 1,374 of our rated ISINs are sub-investment
# grade, and collapsing them into the single "unrated" bucket would value defaulted paper (625
# ISINs at D) the same as a bond nobody happened to rate.
DEFAULT_RATING_SPREAD_BP: Final[dict[str, float]] = {
    "AAA": 50.0,
    "AA+": 60.0,
    "AA": 70.0,
    "AA-": 85.0,
    "A+": 105.0,
    "A": 125.0,
    "A-": 150.0,
    "BBB+": 180.0,
    "BBB": 215.0,
    "BBB-": 255.0,
    "BB+": 320.0,
    "BB": 400.0,
    "BB-": 500.0,
    "B+": 620.0,
    "B": 750.0,
    "B-": 900.0,
    "CCC": 1100.0,
    "CC": 1300.0,
    "C+": 1400.0,
    "C": 1500.0,
    "D": 2000.0,
}
"""Rating -> spread over the CG curve at equivalent maturity, in basis points."""

_UNRATED_SPREAD_BP: Final = 300.0
"""Clause 78(1)(i)(b): an unrated bond must be valued no higher than a rated one of equivalent
maturity. Set at the BBB-/BB+ boundary: wide enough to satisfy the clause against every
investment-grade bucket, without asserting that an unrated bond is distressed."""

# Rating-shaped tokens: longest letter-run first so "AAA" is not read as "AA", with the +/-
# modifier captured separately.
#
# Two anchors, each earning its place. The **leading** \b stops the bare "A" alternative from
# matching the A inside "WITHDRAWN" — without it, 143 withdrawn-rating ISINs valued as single-A
# paper. The **trailing** (?![A-Z0-9]) rejects a longer word that merely starts like a rating,
# and must come *after* the modifier: a trailing \b there fails on "AA+" (both "+" and the next
# character are non-word, so there is no boundary between them), which silently dropped every
# +/- notch — "CRISIL AA+" read as AA, a 10bp error in the bank's favour on 760 ISINs.
_RATING_TOKEN_RE: Final = re.compile(r"\b(AAA|BBB|CCC|AA|BB|CC|A|B|C|D)([+-]?)(?![A-Z0-9])")
# Status markers agencies publish in the rating field. They are the *absence* of a rating, not a
# low one, and must not be pattern-matched into a notch.
_NON_RATINGS: Final = frozenset(
    {"WITHDRAWN", "SUSPENDED", "UNRATED", "NOT RATED", "NA", "N.A.", "NR", "0", "-", ""}
)


def normalize_rating(raw: str | None) -> str | None:
    """Reduce a published long-term rating to its bare notch (``CRISIL AA+`` -> ``AA+``).

    Agencies prefix their own name and suffix outlooks, structure tags and product markers
    ("IND AA+/Stable", "[ICRA]AA-(CE)", "PP-MLD A+"), none of which changes the notch the
    valuation grid is keyed on. Returns ``None`` for a withdrawn, suspended or absent rating —
    which the caller must treat as unrated, not as investment grade.
    """
    if raw is None:
        return None
    text = " ".join(raw.upper().split())
    for token in ("(CE)", "(SO)", "(CG)"):
        text = text.replace(token, "")
    text = text.split("/")[0].strip()
    if text in _NON_RATINGS:
        return None
    match = _RATING_TOKEN_RE.search(text)
    return f"{match.group(1)}{match.group(2)}" if match else None


@dataclass(frozen=True, slots=True)
class Valuation:
    """One security's computed valuation and the rule that produced it."""

    isin: str
    as_of: dt.date
    yield_pct: float
    """The valuation yield: base curve yield plus the applied spread."""
    base_curve_yield_pct: float
    """The central-government yield at equivalent maturity, before any spread."""
    spread_bp: float
    """The spread applied, in basis points."""
    residual_maturity_years: float
    rule: str
    """Which prescription produced this — for the audit trail a bank has to keep."""
    capped_by_trade: bool = False
    """Whether a traded price from the last 15 days overrode the computed figure."""
    traded_yield_pct: float | None = None
    """The capping trade's yield, when one applied."""


def residual_maturity_years(maturity_date: dt.date, as_of: dt.date) -> float:
    """Residual maturity in years (ACT/365), floored at zero for matured paper."""
    return max((maturity_date - as_of).days / 365.0, 0.0)


def price_sovereign(
    *,
    isin: str,
    as_of: dt.date,
    maturity_date: dt.date,
    sovereign_class: SovereignClass,
    cg_curve: Curve,
    spread_bp: float | None = None,
) -> Valuation | None:
    """Value special sovereign paper as CG-at-equivalent-maturity plus its class spread.

    Returns ``None`` for classes the Directions price off the published FBIL mark instead
    (plain stock, SDLs, UDAY, FRB, IIB) — for those the answer is a lookup in ``valuations``,
    not a calculation, and returning a computed number would invite using the wrong one.

    ``spread_bp`` overrides the class default; use it for a DISCOM bond whose state-guarantee
    status is known (:func:`bonds.sovereign.discom_spread_bp`).
    """
    applied = RBI_SPREAD_BP.get(sovereign_class) if spread_bp is None else spread_bp
    if applied is None:
        return None
    tenor = residual_maturity_years(maturity_date, as_of)
    base = cg_curve.yield_at(tenor)
    return Valuation(
        isin=isin,
        as_of=as_of,
        yield_pct=base + applied / 100.0,
        base_curve_yield_pct=base,
        spread_bp=applied,
        residual_maturity_years=tenor,
        rule=f"sovereign:{sovereign_class.value}:CG+{applied:g}bp",
    )


def price_corporate(
    *,
    isin: str,
    as_of: dt.date,
    maturity_date: dt.date,
    rating: str | None,
    cg_curve: Curve,
    traded_yield_pct: float | None = None,
    rating_spread_bp: dict[str, float] | None = None,
) -> Valuation:
    """Value a corporate bond per clause 78(1)(i): CG at equivalent maturity + rating spread.

    ``traded_yield_pct`` is the yield of a print from within the last
    :data:`TRADED_PRICE_CAP_DAYS` days, if any. Clause 78(1)(i)(c) makes such a trade a **cap on
    value**, i.e. a floor on yield: where the traded yield is *higher* than the computed one, the
    traded yield wins. A trade that would raise the valuation does not override the grid.

    An unrated bond takes :data:`_UNRATED_SPREAD_BP`, satisfying clause 78(1)(i)(b) — never
    valued above a rated bond of equivalent maturity.
    """
    grid = rating_spread_bp or DEFAULT_RATING_SPREAD_BP
    notch = normalize_rating(rating)
    spread = grid.get(notch, _UNRATED_SPREAD_BP) if notch else _UNRATED_SPREAD_BP
    spread = max(spread, MIN_CORPORATE_SPREAD_BP)
    tenor = residual_maturity_years(maturity_date, as_of)
    base = cg_curve.yield_at(tenor)
    computed = base + spread / 100.0
    rule = f"corporate:{notch or 'UNRATED'}:CG+{spread:g}bp"

    if traded_yield_pct is not None and traded_yield_pct > computed:
        return Valuation(
            isin=isin,
            as_of=as_of,
            yield_pct=traded_yield_pct,
            base_curve_yield_pct=base,
            spread_bp=(traded_yield_pct - base) * 100.0,
            residual_maturity_years=tenor,
            rule=f"{rule}; capped by {TRADED_PRICE_CAP_DAYS}d traded yield",
            capped_by_trade=True,
            traded_yield_pct=traded_yield_pct,
        )
    return Valuation(
        isin=isin,
        as_of=as_of,
        yield_pct=computed,
        base_curve_yield_pct=base,
        spread_bp=spread,
        residual_maturity_years=tenor,
        rule=rule,
        traded_yield_pct=traded_yield_pct,
    )
