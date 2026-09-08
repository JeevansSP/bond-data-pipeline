"""ISIN check-digit validation (ISO 6166).

The check digit is the trailing Luhn digit over the ISIN with letters expanded to numbers
(A=10 ... Z=35). The rightmost (check) digit is NOT doubled; doubling alternates leftward.
Verified against known-good vectors (e.g. ``US0378331005``, ``GB0002634946``).
"""

from __future__ import annotations

import re
from typing import Final

# ISO 6166 shape: 2-letter country code, 9 alphanumeric NSIN chars, 1 check DIGIT — uppercase
# only. The strict ASCII shape also guards the Luhn loop: Unicode digit-lookalikes ('²' is
# alnum but not int()-able) and lowercase letters (which expand to wrong values) are rejected
# up front instead of crashing or passing by coincidence.
_ISIN_RE: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def has_isin_shape(isin: str) -> bool:
    """Return ``True`` if ``isin`` has the ISO 6166 *shape* (ignoring the check digit).

    Separated from the check digit deliberately. Every identifier our sources publish has the
    right shape; the ones that fail validation fail only on the check digit, and they are real
    securities as published (FBIL prints ``IN1520250085``, NSE prints ``INEO81J07036`` with a
    letter O for a zero). Rejecting those would drop tradeable paper from the master, so a shape
    violation — truncation, lowercase, junk characters, a garbled column — is the ERROR, and a
    bad check digit is a WARN we surface with the offending identifiers.
    """
    return len(isin) == 12 and _ISIN_RE.fullmatch(isin) is not None


def is_valid_isin(isin: str) -> bool:
    """Return ``True`` if ``isin`` is 12 chars with a correct ISO 6166 check digit."""
    if not has_isin_shape(isin):
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
