"""Tests for ISIN check-digit validation."""

from __future__ import annotations

import pytest

from bonds.quality import is_valid_isin


@pytest.mark.parametrize(
    ("isin", "expected"),
    [
        ("US0378331005", True),  # Apple — known valid
        ("GB0002634946", True),  # BAE — known valid
        ("INE002A07809", True),  # Reliance NCD
        ("IN0020160035", True),  # G-Sec
        ("US0378331006", False),  # wrong check digit
        ("INE002A0780", False),  # too short
        ("IN0020160035X", False),  # too long
        ("1234567890AB", False),  # bad country prefix
    ],
)
def test_is_valid_isin(isin: str, expected: bool) -> None:
    assert is_valid_isin(isin) is expected
