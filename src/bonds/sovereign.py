"""Sovereign security classification and the RBI valuation add-ons keyed to it.

The RBI (Classification, Valuation and Operation of Investment Portfolio) Directions, 2025 do
not price every sovereign security off the same curve. Plain central paper and SDLs take the
published FBIL mark; UDAY bonds are priced off FBIL directly; DISCOM, state-serviced and the
GoI "special security" family (oil, fertiliser, FCI, recapitalisation) each take the central
government yield at equivalent maturity **plus a prescribed spread**. Applying the right add-on
needs the security's *class*, and no source publishes it as a field — it is only ever visible in
the description text ("06.13 Special GOI Security 2030", "07.67 TN UDAY 2023", "07.95 FERT BND
2026").

This module is that classifier, plus the spread table the Directions attach to each class.
Clause references are to the Commercial Bank Directions (RBI/DOR/2025-26/162); the other seven
bank-class Directions carry the same rules at shifted numbers — see
``docs/business/2026-07-27_222030_rbi-investment-directions-demand-map.md``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from bonds.states import is_central_sovereign_isin


class SovereignClass(StrEnum):
    """Valuation-relevant class of a sovereign security."""

    PLAIN = "PLAIN"
    """Ordinary dated central-government stock, SDL or SGS — the published FBIL mark applies."""
    UDAY = "UDAY"
    """Ujwal DISCOM Assurance Yojana bond. Priced off FBIL directly (clause 78(2)(i))."""
    STATE_SPECIAL = "STATE_SPECIAL"
    """State "SDL SPECIAL"/"SDL SPL" paper serviced from the state's own budget."""
    DISCOM = "DISCOM"
    """Distribution-company bond (published as "DCMB")."""
    SPECIAL_GOI = "SPECIAL_GOI"
    """GoI "Special Security" — the generic label FBIL uses on its Special sheet."""
    OIL = "OIL"
    """Oil-marketing-company special bond."""
    FCI = "FCI"
    """Food Corporation of India special bond."""
    FERTILISER = "FERTILISER"
    """Fertiliser-company special bond."""
    RECAP = "RECAP"
    """Bank recapitalisation bond."""
    FRB = "FRB"
    """GoI floating-rate bond — not a special security, but not fixed-coupon either."""
    IIB = "IIB"
    """Inflation-indexed bond."""


# Ordered longest-signal-first: "UP SDL SPECIAL" must not fall through to PLAIN, and "RAJASTHAN
# SDL UDAY" must read as UDAY rather than STATE_SPECIAL. Patterns run against the upper-cased
# description; the first match wins.
_CLASS_PATTERNS: Final[tuple[tuple[SovereignClass, re.Pattern[str]], ...]] = (
    (SovereignClass.UDAY, re.compile(r"UDAY")),
    (SovereignClass.DISCOM, re.compile(r"\bDCMB\b|DISCOM")),
    (SovereignClass.RECAP, re.compile(r"RECAP|\bRECP\b")),
    (SovereignClass.OIL, re.compile(r"\bOIL\b")),
    (SovereignClass.FCI, re.compile(r"\bFCI\b|FOOD CORP")),
    (SovereignClass.FERTILISER, re.compile(r"\bFERT")),
    (SovereignClass.IIB, re.compile(r"\bIIB\b|INFLATION")),
    (SovereignClass.FRB, re.compile(r"FLOATING RATE|\bFRB\b")),
    (SovereignClass.SPECIAL_GOI, re.compile(r"SPECIAL GOI|GOI SPECIAL|\bSPL\b|\bSPECIAL\b")),
)
# STATE_SPECIAL is SPECIAL_GOI's state counterpart: the same "SPL"/"SPECIAL" signal on a state
# ISIN. Resolved after pattern matching, where the ISIN is known.
_STATE_SPECIAL_SOURCES: Final = frozenset({SovereignClass.SPECIAL_GOI})


def classify_sovereign(description: str | None, isin: str | None = None) -> SovereignClass:
    """Classify a sovereign security from its published description.

    ``isin`` disambiguates the "special" family: the same ``SPL``/``SPECIAL`` token means a GoI
    special security on central paper (``IN00…``) and state-serviced paper on a state ISIN
    (``IN10…``-``IN49…``). Pass it whenever it is known.

    An unrecognised description is :attr:`SovereignClass.PLAIN` — the overwhelming majority, and
    the class whose valuation rule is simply "use the published mark", so a misclassification
    here is a no-op rather than a wrong spread.
    """
    text = (description or "").upper()
    for sovereign_class, pattern in _CLASS_PATTERNS:
        if pattern.search(text):
            if (
                sovereign_class in _STATE_SPECIAL_SOURCES
                and isin
                and not is_central_sovereign_isin(isin)
            ):
                return SovereignClass.STATE_SPECIAL
            return sovereign_class
    return SovereignClass.PLAIN


# Spread over the central-government yield at equivalent maturity, in basis points, per the
# Commercial Bank Directions. ``None`` means "no curve-plus-spread rule applies": use the
# published FBIL mark for that security instead.
#
#   clause 78(2)(i)    UDAY               -> FBIL price directly
#   clause 78(2)(ii)   DISCOM, state-guaranteed        -> CG + 75bp
#   clause 78(2)(iii)  DISCOM, not state-guaranteed    -> CG + 100bp
#   clause 78(2)(iv)   state-serviced                  -> CG + 50bp
#   clause 78(3)       special GoI securities          -> CG + 25bp
#   clause 77          other approved securities       -> CG + 25bp
RBI_SPREAD_BP: Final[dict[SovereignClass, float | None]] = {
    SovereignClass.PLAIN: None,
    SovereignClass.UDAY: None,
    SovereignClass.STATE_SPECIAL: 50.0,
    SovereignClass.DISCOM: 100.0,
    SovereignClass.SPECIAL_GOI: 25.0,
    SovereignClass.OIL: 25.0,
    SovereignClass.FCI: 25.0,
    SovereignClass.FERTILISER: 25.0,
    SovereignClass.RECAP: 25.0,
    SovereignClass.FRB: None,
    SovereignClass.IIB: None,
}

DISCOM_STATE_GUARANTEED_SPREAD_BP: Final = 75.0
"""Clause 78(2)(ii). :data:`RBI_SPREAD_BP` carries the *un*guaranteed 100bp for DISCOM because
nothing in the feeds says whether a given DCMB carries a state guarantee. 100bp is the
conservative (lower-value) reading; a holder who knows the guarantee status should override to
this figure. See :func:`discom_spread_bp`."""


def discom_spread_bp(*, state_guaranteed: bool) -> float:
    """DISCOM spread over the CG curve: 75bp if state-guaranteed, else 100bp."""
    return DISCOM_STATE_GUARANTEED_SPREAD_BP if state_guaranteed else 100.0
