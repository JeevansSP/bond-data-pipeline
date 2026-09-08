"""Canonical Indian state/UT vocabulary for sovereign (SDL/SGS) issuer identity.

One shared vocabulary for every connector that derives a state issuer, so FBIL and CCIL cannot
drift apart. They previously did: CCIL normalised to full state names via its own alias table
while the FBIL valuation pipeline emitted the raw two-letter code from the description, so the
same issuer arrived as both ``State Government (AP)`` and ``State Government (ANDHRA PRADESH)``
and the master fragmented into 63 issuer strings. A one-off SQL repair fixed the rows without
unifying the two code paths, so the next nightly FBIL load re-introduced the codes.

The canonical form is the **full upper-case state name** — two-letter codes are ambiguous in the
published feeds (FBIL prints both ``ML``/``MG`` for Meghalaya, ``NL``/``NG`` for Nagaland and
``PY``/``PD`` for Puducherry; verified as the same issuer by SDL ISIN prefix — IN24, IN26 and
IN38 respectively) and unreadable in a customer-facing feed.

:data:`CANONICAL_STATES` is a **closed** vocabulary: ``bonds dq assess`` fails on any SDL issuer
outside it, which is what makes a regression like the above visible the night it happens.
"""

from __future__ import annotations

import re
from typing import Final

from bonds.logging import get_logger

logger = get_logger(__name__)

# SDL descriptions carry the state name between the coupon and a drift-prone marker. Across
# 24 years of naming the marker appears as SDL / SGS / SGL / UDAY / GS / G.S. / G S / S.D.(L.) /
# GOVT(. STOCK) / LOAN — or not at all, in which case the 4-digit maturity year bounds the state
# ("10.50% JAMMU & KASHMIR 2011"). Validated against every distinct SDL description in the
# landed history: 31 canonical issuers, zero unmatched.
SDL_MARKER: Final = (
    r"(?:SDL|SGS|SGL|UDAY|GS|G\.?\s?S\.?|S\.?\s?D\.?L?\.?|GOVT\.?\s*(?:STOCK)?|STOCK|LOAN)"
)
_SDL_STATE_RE: Final = re.compile(
    rf"^\s*[\d.]+\s*%?\s*(?P<state>[A-Z][A-Z .&]*?)\s*(?:\b{SDL_MARKER}(?=[\s\d.(]|$)|\d{{4}})"
)

# Observed spelling variants -> canonical state, keyed on the squashed form (A-Z and & only, so
# "A. P.", "A.P" and "AP" share a key). An unrecognised spelling passes through this lookup
# unchanged; sdl_issuer then declines to build an issuer from it (logging a warning), so a new
# state spelling shows up as a NULL issuer the completeness check reports — never as a new
# issuer string that quietly re-fragments the master.
_SPELLING_ALIASES: Final[dict[str, str]] = {
    "ANDHRA": "ANDHRA PRADESH",
    "ANDHRAPRADESH": "ANDHRA PRADESH",
    "ARPR": "ARUNACHAL PRADESH",
    "ARUNACHAL": "ARUNACHAL PRADESH",
    "ARUNACHALPR": "ARUNACHAL PRADESH",
    "ARUNACHALPRA": "ARUNACHAL PRADESH",
    "ARUNACHALPRADESH": "ARUNACHAL PRADESH",
    "ARUNPRA": "ARUNACHAL PRADESH",
    "CHATISGAR": "CHHATTISGARH",
    "CHATTIS": "CHHATTISGARH",
    "CHATTISGARH": "CHHATTISGARH",
    "CHHATISGARH": "CHHATTISGARH",
    "CHHATISHGARH": "CHHATTISGARH",
    "GUJRAT": "GUJARAT",
    "HARAYANA": "HARYANA",
    "HIMACHAL": "HIMACHAL PRADESH",
    "HIMACHALPR": "HIMACHAL PRADESH",
    "HIMACHALPRADESH": "HIMACHAL PRADESH",
    "HIMACHALPRADESHSDL": "HIMACHAL PRADESH",
    "J&K": "JAMMU & KASHMIR",
    "JAMMU&KASHMIR": "JAMMU & KASHMIR",
    "JAMMUANDKASHMIR": "JAMMU & KASHMIR",
    "JAMMUKASHMIR": "JAMMU & KASHMIR",
    "JAMMUKASMIR": "JAMMU & KASHMIR",
    "JHARKAND": "JHARKHAND",
    "KARN": "KARNATAKA",
    "KARNATAK": "KARNATAKA",
    "KER": "KERALA",
    "KERELA": "KERALA",
    "MADHYAPR": "MADHYA PRADESH",
    "MADHYAPRADESH": "MADHYA PRADESH",
    "MAH": "MAHARASHTRA",
    "MAHA": "MAHARASHTRA",
    "MAHARASTRA": "MAHARASHTRA",
    "MAHRASTRA": "MAHARASHTRA",
    "MANI": "MANIPUR",
    "MEGH": "MEGHALAYA",
    "MEGHALAY": "MEGHALAYA",
    "ORISSA": "ODISHA",
    "ORRISA": "ODISHA",
    "PONDICHERRY": "PUDUCHERRY",
    "RAJ": "RAJASTHAN",
    "RAJSTHAN": "RAJASTHAN",
    "TAMILNADU": "TAMIL NADU",
    "TELENGANA": "TELANGANA",
    "UTRANCHAL": "UTTARAKHAND",
    "UTRRANCHAL": "UTTARAKHAND",
    "UTTARANCHAL": "UTTARAKHAND",
    "UTTARC": "UTTARAKHAND",
    "UTTARPRADESH": "UTTAR PRADESH",
    "UTTRANCHAL": "UTTARAKHAND",
    "WBENGAL": "WEST BENGAL",
    "WESTBENGAL": "WEST BENGAL",
}

# Two-letter codes as printed by FBIL SDL/SGS descriptions. Kept separate from the spelling
# variants above only for readability; they are merged into one lookup below.
_CODE_ALIASES: Final[dict[str, str]] = {
    "AP": "ANDHRA PRADESH",
    "AR": "ARUNACHAL PRADESH",
    "AS": "ASSAM",
    "BR": "BIHAR",
    "CG": "CHHATTISGARH",
    "DL": "DELHI",
    "GA": "GOA",
    "GJ": "GUJARAT",
    "HP": "HIMACHAL PRADESH",
    "HR": "HARYANA",
    "JH": "JHARKHAND",
    "JK": "JAMMU & KASHMIR",
    "KA": "KARNATAKA",
    "KL": "KERALA",
    "MG": "MEGHALAYA",
    "MH": "MAHARASHTRA",
    "ML": "MEGHALAYA",
    "MN": "MANIPUR",
    "MP": "MADHYA PRADESH",
    "MZ": "MIZORAM",
    "NG": "NAGALAND",
    "NL": "NAGALAND",
    "OD": "ODISHA",
    "PD": "PUDUCHERRY",
    "PN": "PUNJAB",
    "PY": "PUDUCHERRY",
    "RJ": "RAJASTHAN",
    "SK": "SIKKIM",
    "TN": "TAMIL NADU",
    "TR": "TRIPURA",
    "TS": "TELANGANA",
    "UK": "UTTARAKHAND",
    "UP": "UTTAR PRADESH",
    "WB": "WEST BENGAL",
}

STATE_ALIASES: Final[dict[str, str]] = {**_SPELLING_ALIASES, **_CODE_ALIASES}

CANONICAL_STATES: Final[frozenset[str]] = frozenset(STATE_ALIASES.values())
"""The closed set of state/UT issuers seen in the sovereign feeds (31 of them)."""

SDL_ISSUERS: Final[frozenset[str]] = frozenset(
    f"State Government ({state})" for state in CANONICAL_STATES
)
"""Every legal ``securities.issuer`` value for an SDL — the DQ check's allowed vocabulary."""


# Sovereign ISIN issuer form (structural, so it outranks a mis-titled worksheet):
# central government paper is IN00…, state paper IN10…-IN49…. Corporate ISINs (INE…/INF…)
# match neither.
_CENTRAL_SOVEREIGN_ISIN_RE: Final = re.compile(r"^IN00")
_STATE_SOVEREIGN_ISIN_RE: Final = re.compile(r"^IN[1-4][0-9]")


def is_central_sovereign_isin(isin: str) -> bool:
    """Whether ``isin`` is central-government sovereign paper (G-Sec/T-Bill/STRIPS/SGB)."""
    return _CENTRAL_SOVEREIGN_ISIN_RE.match(isin) is not None


def is_state_sovereign_isin(isin: str) -> bool:
    """Whether ``isin`` is state/UT sovereign paper (SDL/SGS/UDAY).

    Used to correct an instrument type derived from a worksheet title: FBIL's pre-Feb-2023
    combined workbooks carry state UDAY and SDL-SPL bonds on the G-Sec workbook's "Special"
    sheet, which the title map classifies as central. The ISIN is authoritative.
    """
    return _STATE_SOVEREIGN_ISIN_RE.match(isin) is not None


def canonical_state(raw: str) -> str:
    """Map a published state spelling or code to its canonical full name.

    Unknown input passes through unchanged (upper-cased, punctuation-squashed lookup), so a new
    state spelling is surfaced by the DQ vocabulary check rather than silently discarded.
    """
    return STATE_ALIASES.get(re.sub(r"[^A-Z&]", "", raw.upper()), raw)


def sdl_issuer(description: str | None) -> str | None:
    """Derive the ``State Government (<STATE>)`` issuer from an SDL/SGS/UDAY description.

    Returns ``None`` when the description carries no recognisable state — an unknown issuer,
    never a wrong "Government of India".
    """
    match = _SDL_STATE_RE.match((description or "").strip().upper())
    if not match:
        return None
    state = canonical_state(" ".join(match.group("state").split()))
    if state not in CANONICAL_STATES:
        # The pattern also matches central descriptors ("06.94 GOVT. STOCK 2036" -> "GOVT."),
        # and a genuinely new state spelling would otherwise be written as a junk issuer that
        # fragments the master. An unknown issuer is a NULL, surfaced by the DQ completeness
        # check, never a guess.
        logger.warning("states.unrecognised_state", description=description, parsed=state)
        return None
    return f"State Government ({state})"
