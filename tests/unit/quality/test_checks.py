"""Tests for the batch quality checks."""

from __future__ import annotations

import datetime as dt

import pytest

from bonds.models import InstrumentType, SecurityRecord, SovereignValuation
from bonds.quality.checks import Level, QualityCheck, check_universe, check_valuations

DATE = dt.date(2026, 7, 10)


def _val(isin: str, price: float | None, ytm: float | None) -> SovereignValuation:
    return SovereignValuation(
        isin=isin,
        quote_date=DATE,
        instrument_type=InstrumentType.GSEC,
        source="fbil",
        price=price,
        ytm=ytm,
    )


def _find(checks: list[QualityCheck], name: str) -> QualityCheck:
    return next(c for c in checks if c.name == name)


def test_check_valuations_all_clean() -> None:
    checks = check_valuations([_val("IN0020160035", 100.2, 5.3), _val("IN0020230119", 99.8, 7.1)])
    assert all(c.passed for c in checks)


def test_check_valuations_flags_anomalies() -> None:
    checks = check_valuations(
        [
            _val("IN0020160035", 100.0, 6.0),
            _val("IN0020230119", 300.0, 6.0),  # price out of range
            _val("IN0020210186", 100.0, 30.0),  # ytm out of range
            _val("IN0020010081", None, None),  # null price + ytm
        ]
    )
    assert _find(checks, "price_out_of_range").observed == 1.0
    assert not _find(checks, "price_out_of_range").passed
    assert _find(checks, "ytm_out_of_range").observed == 1.0
    assert _find(checks, "null_price_rate").observed == pytest.approx(0.25)
    assert not _find(checks, "null_price_rate").passed


def test_check_valuations_invalid_isin_is_error() -> None:
    checks = check_valuations([_val("IN0020160034", 100.0, 6.0)])  # bad check digit
    invalid = _find(checks, "invalid_isin")
    assert invalid.observed == 1.0
    assert invalid.level is Level.ERROR and not invalid.passed


def test_check_valuations_empty_batch_fails_row_count() -> None:
    checks = check_valuations([])
    assert not _find(checks, "row_count").passed


def test_check_universe_counts_matured() -> None:
    records = [
        SecurityRecord(
            isin="INE002A07809",
            instrument_type=InstrumentType.CORP,
            source="bondcentral",
            maturity_date=dt.date(2020, 1, 1),  # matured before as_of
        ),
        SecurityRecord(
            isin="IN8241O08017",
            instrument_type=InstrumentType.CORP,
            source="bondcentral",
            maturity_date=dt.date(2030, 1, 1),
        ),
    ]
    checks = check_universe(records, as_of=DATE)
    assert _find(checks, "matured_in_universe").observed == 1.0
    assert _find(checks, "invalid_isin").passed
