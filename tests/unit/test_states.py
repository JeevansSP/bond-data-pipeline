"""Tests for the shared sovereign state/UT vocabulary."""

from __future__ import annotations

import pytest

from bonds.states import (
    CANONICAL_STATES,
    SDL_ISSUERS,
    canonical_state,
    is_central_sovereign_isin,
    is_state_sovereign_isin,
    sdl_issuer,
)


def test_vocabulary_is_closed_and_consistent() -> None:
    assert len(CANONICAL_STATES) == 31
    assert {f"State Government ({s})" for s in CANONICAL_STATES} == SDL_ISSUERS


@pytest.mark.parametrize(
    ("description", "issuer"),
    [
        # FBIL prints two-letter codes; CCIL prints full names. Both must land on one issuer.
        ("07.83 GJ SDL 2026", "State Government (GUJARAT)"),
        ("7.83% GUJARAT G.S. 2012", "State Government (GUJARAT)"),
        # UT paper uses SGS rather than SDL.
        ("07.31 DL SGS 2033", "State Government (DELHI)"),
        ("06.42 JK SGS 2031", "State Government (JAMMU & KASHMIR)"),
        # UDAY bonds are state paper; without UDAY in the marker set the greedy state group
        # swallowed it and the issuer came out as "State Government (TN UDAY)".
        ("07.67 TN UDAY 2023", "State Government (TAMIL NADU)"),
        ("08.14 UP UDAY 2026", "State Government (UTTAR PRADESH)"),
        # FBIL prints two codes for three states; the SDL ISIN prefix confirms they are the
        # same issuer (MG/ML -> IN24, NG/NL -> IN26, PD/PY -> IN38).
        ("07.26 MG SDL 2027", "State Government (MEGHALAYA)"),
        ("04.69 ML SDL 2023", "State Government (MEGHALAYA)"),
        ("07.22 NG SDL 2026", "State Government (NAGALAND)"),
        ("06.50 NL SDL 2030", "State Government (NAGALAND)"),
        ("07.88 PD SDL 2028", "State Government (PUDUCHERRY)"),
        ("05.07 PY SDL 2023", "State Government (PUDUCHERRY)"),
        # 24 years of CCIL spelling drift.
        ("13.05% ORISSA GOVT LOAN 2007", "State Government (ODISHA)"),
        ("10.50% JAMMU & KASHMIR 2011", "State Government (JAMMU & KASHMIR)"),
        ("7.80% A.P. SDL(APL) 2012", "State Government (ANDHRA PRADESH)"),
        ("8.00% MAHRASTRA GS 2018", "State Government (MAHARASHTRA)"),
        # Central paper matches the shape ("GOVT." looks like a state) but must never yield a
        # state issuer — it previously produced "State Government (GOVT.)".
        ("06.94 GOVT. STOCK 2036", None),
        ("91 TBILL MATURING 12/04/2002", None),
        ("garbage with no state marker", None),
        (None, None),
    ],
)
def test_sdl_issuer(description: str | None, issuer: str | None) -> None:
    assert sdl_issuer(description) == issuer


def test_sdl_issuer_only_emits_canonical_issuers() -> None:
    for desc in ("07.83 GJ SDL 2026", "13.05% ORISSA GOVT LOAN 2007", "07.67 TN UDAY 2023"):
        derived = sdl_issuer(desc)
        assert derived is not None and derived in SDL_ISSUERS


def test_canonical_state_passes_unknown_through() -> None:
    # canonical_state is the raw lookup; the closed-vocabulary guard lives in sdl_issuer.
    assert canonical_state("ap") == "ANDHRA PRADESH"
    assert canonical_state("ATLANTIS") == "ATLANTIS"


@pytest.mark.parametrize(
    ("isin", "central", "state"),
    [
        ("IN0020260025", True, False),  # central G-Sec
        ("IN002001X091", True, False),  # T-Bill
        ("IN000724C022", True, False),  # STRIPS
        ("IN1520250085", False, True),  # SDL
        ("IN3120160078", False, True),  # state UDAY (mis-typed GSEC before the guard)
        ("INE013A07317", False, False),  # corporate matches neither
    ],
)
def test_sovereign_isin_form(isin: str, central: bool, state: bool) -> None:
    assert is_central_sovereign_isin(isin) is central
    assert is_state_sovereign_isin(isin) is state
