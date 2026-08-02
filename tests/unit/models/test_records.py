"""Tests for domain record validation."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from bonds.models import (
    InstrumentType,
    SecurityRecord,
    SovereignValuation,
    normalize_interest_type,
)


def test_valuation_is_frozen() -> None:
    v = SovereignValuation(
        isin="IN0020160035",
        quote_date=dt.date(2026, 7, 10),
        instrument_type=InstrumentType.GSEC,
        source="fbil",
    )
    with pytest.raises(ValidationError):
        v.price = 100.0


def test_valuation_rejects_bad_isin_length() -> None:
    with pytest.raises(ValidationError):
        SovereignValuation(
            isin="TOO_SHORT",
            quote_date=dt.date(2026, 7, 10),
            instrument_type=InstrumentType.SDL,
            source="fbil",
        )


def test_security_record_defaults_optional_fields_to_none() -> None:
    r = SecurityRecord(isin="IN1520160061", instrument_type=InstrumentType.SDL, source="fbil")
    assert r.coupon is None
    assert r.maturity_date is None
    assert r.issuer is None


def test_negative_coupon_coerced_to_none() -> None:
    # Matches ck_security_coupon_nonneg so a bad coupon nulls out instead of failing the batch.
    r = SecurityRecord(
        isin="IN1520160061", instrument_type=InstrumentType.SDL, source="cdsl", coupon=-3.0
    )
    assert r.coupon is None
    ok = SecurityRecord(
        isin="IN1520160061", instrument_type=InstrumentType.SDL, source="cdsl", coupon=0.0
    )
    assert ok.coupon == 0.0  # zero-coupon is valid


@pytest.mark.parametrize(
    ("maturity", "expected"),
    [
        (dt.date(1999, 12, 31), None),  # CDSL no-maturity sentinel
        (dt.date(2999, 12, 31), None),  # perpetual-bond sentinel
        (dt.date(2099, 12, 31), None),  # perpetual-bond sentinel
        (dt.date(1934, 1, 9), None),  # junk
        (dt.date(2002, 3, 21), dt.date(2002, 3, 21)),  # real 2002 T-Bill maturity kept
        (dt.date(2065, 6, 1), dt.date(2065, 6, 1)),  # real long-dated G-Sec kept
        (None, None),
    ],
)
def test_implausible_maturity_coerced_to_none(
    maturity: dt.date | None, expected: dt.date | None
) -> None:
    r = SecurityRecord(
        isin="IN1520160061",
        instrument_type=InstrumentType.CORP,
        source="cdsl",
        maturity_date=maturity,
    )
    assert r.maturity_date == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # FIXED bucket
        ("Fixed", "FIXED"),
        ("FIXED", "FIXED"),
        ("Fixed Interest", "FIXED"),
        # ZERO bucket
        ("ZERO_COUPON", "ZERO"),
        ("Zero Interest", "ZERO"),
        ("No Interest", "ZERO"),
        ("Zero", "ZERO"),
        # FLOATING bucket (rate/index/inflation-linked floaters)
        ("Floating", "FLOATING"),
        ("Variable Interest", "FLOATING"),
        ("Variable-Others", "FLOATING"),
        ("Variable-Index Linked", "FLOATING"),
        ("Variable - Mibor Linked", "FLOATING"),
        ("Variable-Inflation", "FLOATING"),
        # OTHER bucket (market-linked debentures + explicit N/A)
        ("Variable-Equity Linked", "OTHER"),
        ("Variable- Commodity linked", "OTHER"),
        ("Not Applicable", "OTHER"),
        # Unknown non-empty vocabulary must never crash: it maps to OTHER
        ("Step-Up Coupon", "OTHER"),
        ("X" * 60, "OTHER"),  # would previously have needed VARCHAR(48) truncation
        # Whitespace robustness
        ("  fixed  interest ", "FIXED"),
        # NULL/empty stays None
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_normalize_interest_type(raw: str | None, expected: str | None) -> None:
    assert normalize_interest_type(raw) == expected


def test_security_record_normalizes_interest_type() -> None:
    r = SecurityRecord(
        isin="IN1520160061",
        instrument_type=InstrumentType.CORP,
        source="bondcentral",
        interest_type="Fixed Interest",
    )
    assert r.interest_type == "FIXED"
    unknown = SecurityRecord(
        isin="IN1520160061",
        instrument_type=InstrumentType.CORP,
        source="bondcentral",
        interest_type="Brand New Category",
    )
    assert unknown.interest_type == "OTHER"
    absent = SecurityRecord(
        isin="IN1520160061", instrument_type=InstrumentType.CORP, source="bondcentral"
    )
    assert absent.interest_type is None
