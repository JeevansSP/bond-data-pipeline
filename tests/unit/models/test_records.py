"""Tests for domain record validation."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from bonds.models import InstrumentType, SecurityRecord, SovereignValuation


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


def test_interest_type_truncated_to_column_width() -> None:
    r = SecurityRecord(
        isin="IN1520160061",
        instrument_type=InstrumentType.CORP,
        source="bondcentral",
        interest_type="X" * 60,  # longer than VARCHAR(48)
    )
    assert r.interest_type is not None and len(r.interest_type) == 48
