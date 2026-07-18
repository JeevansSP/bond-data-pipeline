"""ISIN check-digit validation (ISO 6166).

The check digit is the trailing Luhn digit over the ISIN with letters expanded to numbers
(A=10 ... Z=35). The rightmost (check) digit is NOT doubled; doubling alternates leftward.
Verified against known-good vectors (e.g. ``US0378331005``, ``GB0002634946``).
"""

from __future__ import annotations


def is_valid_isin(isin: str) -> bool:
    """Return ``True`` if ``isin`` is 12 chars with a correct ISO 6166 check digit."""
    if len(isin) != 12 or not isin[:2].isalpha() or not isin[2:].isalnum():
        return False
    expanded = "".join(str(ord(c) - 55) if c.isalpha() else c for c in isin)
    total = 0
    double = False  # the rightmost (check) digit is not doubled
    for char in reversed(expanded):
        value = int(char)
        if double:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        double = not double
    return total % 10 == 0
