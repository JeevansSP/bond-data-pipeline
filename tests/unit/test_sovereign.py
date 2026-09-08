"""Tests for sovereign security classification and its RBI spread add-ons."""

from __future__ import annotations

import pytest

from bonds.sovereign import (
    RBI_SPREAD_BP,
    SovereignClass,
    classify_sovereign,
    discom_spread_bp,
)

CENTRAL_ISIN = "IN0020260025"
STATE_ISIN = "IN3320130011"


@pytest.mark.parametrize(
    ("description", "isin", "expected"),
    [
        # Plain paper is the overwhelming majority and takes the published mark.
        ("07.38 TS SDL 2027", STATE_ISIN, SovereignClass.PLAIN),
        ("06.94 GOVT. STOCK 2036", CENTRAL_ISIN, SovereignClass.PLAIN),
        # UDAY wins over the "SDL" in the same description.
        ("07.67 TN UDAY 2023", STATE_ISIN, SovereignClass.UDAY),
        ("06.83 RAJASTHAN SDL UDAY 2020", STATE_ISIN, SovereignClass.UDAY),
        # The special family.
        ("06.69 Special GOI Security 2031", CENTRAL_ISIN, SovereignClass.SPECIAL_GOI),
        ("08.00 OIL SPL 2026", CENTRAL_ISIN, SovereignClass.OIL),
        ("08.15 GOI FCI 2022", CENTRAL_ISIN, SovereignClass.FCI),
        ("7% FERT COS GOI SPL BOND 2022", CENTRAL_ISIN, SovereignClass.FERTILISER),
        ("010 DCMB 16082019", CENTRAL_ISIN, SovereignClass.DISCOM),
        ("GOI FLOATING RATE BOND 2006", CENTRAL_ISIN, SovereignClass.FRB),
        # The same "SPL"/"SPECIAL" token means GoI special on central paper and state-serviced
        # on a state ISIN — the ISIN is what disambiguates.
        ("08.67 UP SDL SPL 2023", STATE_ISIN, SovereignClass.STATE_SPECIAL),
        ("08.31 UTTAR PRADESH SDL SPECIAL 2019", STATE_ISIN, SovereignClass.STATE_SPECIAL),
        (None, None, SovereignClass.PLAIN),
    ],
)
def test_classify_sovereign(
    description: str | None, isin: str | None, expected: SovereignClass
) -> None:
    assert classify_sovereign(description, isin) is expected


def test_special_token_without_isin_defaults_to_central() -> None:
    # Without an ISIN there is nothing to disambiguate on; the GoI reading is the safe default
    # because it carries the *narrower* spread and so cannot understate a valuation.
    assert classify_sovereign("08.67 UP SDL SPL 2023") is SovereignClass.SPECIAL_GOI


def test_classes_priced_off_fbil_carry_no_spread() -> None:
    # Clause 78(2)(i): UDAY is priced off the published FBIL mark, not off the curve. Returning
    # a spread for it would invite computing a number where a lookup is prescribed.
    for sovereign_class in (SovereignClass.PLAIN, SovereignClass.UDAY, SovereignClass.FRB):
        assert RBI_SPREAD_BP[sovereign_class] is None


def test_special_security_spreads_match_the_directions() -> None:
    assert RBI_SPREAD_BP[SovereignClass.SPECIAL_GOI] == 25.0  # clause 78(3)
    assert RBI_SPREAD_BP[SovereignClass.STATE_SPECIAL] == 50.0  # clause 78(2)(iv)
    assert RBI_SPREAD_BP[SovereignClass.DISCOM] == 100.0  # clause 78(2)(iii)


def test_discom_spread_depends_on_guarantee() -> None:
    # 78(2)(ii) vs 78(2)(iii). The table default is the unguaranteed 100bp because nothing in
    # the feeds says which a given DCMB is, and 100bp is the lower-value reading.
    assert discom_spread_bp(state_guaranteed=True) == 75.0
    assert discom_spread_bp(state_guaranteed=False) == 100.0
