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
