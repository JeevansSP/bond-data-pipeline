"""Tests for the batch quality checks."""

from __future__ import annotations

import datetime as dt

import pytest

from bonds.models import InstrumentType, SecurityRecord, SovereignValuation, TradeRecord
from bonds.quality.checks import (
    Level,
    QualityCheck,
    check_trades,
    check_universe,
    check_valuations,
)

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


def _trade(isin: str, segment: str, ltp: float) -> TradeRecord:
    return TradeRecord(isin=isin, trade_date=DATE, source="ccil", segment=segment, ltp=ltp)


def test_check_trades_price_band_scoped_to_par_priced_segments() -> None:
    # SGB (per gram, ~17950) and STRIPS (deep discount, ~28) price far outside [50,200] but are
    # legitimately priced; only GSEC/SDL/TBILL should count toward ltp_out_of_range.
    trades = [
        _trade("IN0020260025", "GSEC", 101.0),  # in band
        _trade("IN0020210228", "SGB", 17950.0),  # legit high, must NOT flag
        _trade("IN001241C032", "STRIPS", 28.6),  # legit low, must NOT flag
        _trade("IN0020190999", "GSEC", 5.0),  # genuinely bad par bond, MUST flag
    ]
    oob = _find(check_trades(trades), "ltp_out_of_range")
    assert oob.observed == 1.0  # only the bad GSEC
    assert not oob.passed


def test_check_trades_empty_is_info_row_count() -> None:
    (row_count,) = check_trades([])
    assert row_count.name == "row_count" and row_count.observed == 0.0


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
