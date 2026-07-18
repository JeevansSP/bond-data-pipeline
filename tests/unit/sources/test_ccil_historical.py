"""Tests for the CCIL historical-trades connector (AES, CSV aggregation, fetch)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.sources import SourceError
from bonds.sources.ccil_historical import (
    CcilHistoricalTradesSource,
    _as_date,
    _instrument_segment,
    _parse_row,
    _time_key,
    aggregate_trades,
    encrypt_date,
)

_HEADER = (
    "Trade,Trade Time,ISIN,Security Description,Face Value,Trade Price,YTM/Yield,Trade Indicator\n"
)

_CSV = (
    "Trade,Trade Time,ISIN,Security Description,Face Value,Trade Price,YTM/Yield,Trade Indicator\n"
    "17-07-2026,16:59:59,IN0020260025,06.94 GOVT. STOCK 2036,50000000.000,101.1150,6.7806,NRML\n"
    "17-07-2026,16:58:00,IN0020260025,06.94 GOVT. STOCK 2036,100000000.000,101.2000,6.7500,NRML\n"
    "17-07-2026,16:00:00,IN2220190127,06.97 MAHARASHTRA SGS 2028,7500000.000,101.8000,6.2315,NRML\n"
    "17-07-2026,15:00:00,IN0020260099,DTB 15012027,25000000.000,98.5000,5.5000,NRML\n"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_encrypt_date_matches_site_ciphertext() -> None:
    # Reproduces CCIL's encryptDetails byte-for-byte (verified against the live page).
    assert encrypt_date("16/07/2026") == "6AUyCd+B1T3jyWBfbj3Kng=="


def test_aggregate_trades_vwap_count_and_segment() -> None:
    by_isin = {r.isin: r for r in aggregate_trades(_CSV, source="ccil")}
    assert set(by_isin) == {"IN0020260025", "IN2220190127", "IN0020260099"}

    gsec = by_isin["IN0020260025"]
    assert gsec.segment == "GSEC"
    assert gsec.no_of_trades == 2
    assert gsec.trade_value == pytest.approx(150_000_000.0)  # sum of face
    assert gsec.ltp == pytest.approx(101.1150)  # latest trade (16:59:59)
    # VWAP = (101.1150*50M + 101.2000*100M) / 150M
    assert gsec.wap == pytest.approx((101.1150 * 50e6 + 101.2000 * 100e6) / 150e6)

    assert by_isin["IN2220190127"].segment == "SDL"  # "SGS" = State Government Securities
    assert by_isin["IN0020260099"].segment == "TBILL"  # "DTB" = Discounted Treasury Bill


def test_aggregate_empty_or_error_returns_empty() -> None:
    assert aggregate_trades("", source="ccil") == []
    assert aggregate_trades("Undeployed", source="ccil") == []


def test_last_trade_uses_numeric_time_not_string_compare() -> None:
    # Unpadded "9:05:00" lexically sorts AFTER "16:00:00"; the numeric key must rank 16:00 last.
    csv_text = _HEADER + (
        "17-07-2026,9:05:00,IN0020260025,06.94 GS 2036,50000000.000,101.0000,6.80,NRML\n"
        "17-07-2026,16:00:00,IN0020260025,06.94 GS 2036,50000000.000,102.0000,6.70,NRML\n"
    )
    (rec,) = aggregate_trades(csv_text, source="ccil")
    assert rec.ltp == pytest.approx(102.0000)  # the 16:00 close, not the 9:05 morning print
    assert rec.lty == pytest.approx(6.70)


def test_zero_and_missing_values_excluded_from_vwap_but_counted() -> None:
    csv_text = _HEADER + (
        "17-07-2026,10:00:00,IN0020260025,06.94 GS 2036,50000000.000,0.0000,6.80,NRML\n"  # price 0
        "17-07-2026,11:00:00,IN0020260025,06.94 GS 2036,50000000.000,101.0000,6.70,NRML\n"  # valid
        "17-07-2026,12:00:00,IN0020260025,06.94 GS 2036,,101.5000,6.60,NRML\n"  # face missing
    )
    (rec,) = aggregate_trades(csv_text, source="ccil")
    assert rec.no_of_trades == 3  # every trade counts
    assert rec.trade_value == pytest.approx(50_000_000.0)  # only the valid row's face
    assert rec.wap == pytest.approx(101.0000)  # zero/missing rows don't pollute the VWAP
    assert rec.ltp == pytest.approx(101.0000)  # ltp = last VALID-price trade (11:00), not 12:00


def test_group_with_no_valid_prices_yields_none_wap() -> None:
    csv_text = _HEADER + "17-07-2026,10:00:00,IN2220190127,SGS 2028,50000000.000,0,6,NRML\n"
    (rec,) = aggregate_trades(csv_text, source="ccil")
    assert rec.no_of_trades == 1
    assert rec.wap is None and rec.way is None and rec.ltp is None  # no ZeroDivisionError


def test_column_shift_row_is_rejected() -> None:
    # A stray unquoted comma in the description makes this row 9 fields; it must be dropped, not
    # parsed with shifted columns. The clean 8-field row still comes through.
    csv_text = _HEADER + (
        "17-07-2026,16:00:00,IN0020260099,GOI FRB, 2033,50000000.000,101.0,6.8,NRML\n"  # 9 fields
        "17-07-2026,16:00:00,IN0020260025,06.94 GS 2036,50000000.000,101.0,6.8,NRML\n"  # 8 fields
    )
    isins = {r.isin for r in aggregate_trades(csv_text, source="ccil")}
    assert isins == {"IN0020260025"}


@pytest.mark.parametrize(
    ("desc", "segment"),
    [
        ("06.94 GOVT. STOCK 2036", "GSEC"),
        ("DTB 15012027", "TBILL"),
        ("CMB 91 DAYS 2026", "TBILL"),
        ("06.97 MAHARASHTRA SGS 2028", "SDL"),
        ("07.20 KARNATAKA SDL 2030", "SDL"),
        ("SGB 2028 SR-II", "SGB"),
        ("dtb lowercase", "TBILL"),
        (None, "GSEC"),
        ("", "GSEC"),
    ],
)
def test_instrument_segment_classification(desc: str | None, segment: str) -> None:
    assert _instrument_segment(desc) == segment


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("09:05:00", (9, 5, 0)),
        ("9:05:00", (9, 5, 0)),  # unpadded parses the same
        ("16:59:59", (16, 59, 59)),
        ("garbage", (-1, -1, -1)),
        ("aa:bb:cc", (-1, -1, -1)),  # right shape, non-numeric
        ("10:20", (-1, -1, -1)),  # wrong shape
    ],
)
def test_time_key(value: str, expected: tuple[int, int, int]) -> None:
    assert _time_key(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("17-07-2026", dt.date(2026, 7, 17)),  # %d-%m-%Y
        ("2026-07-17", dt.date(2026, 7, 17)),  # %Y-%m-%d
        ("17-Jul-2026", dt.date(2026, 7, 17)),  # %d-%b-%Y
        ("not-a-date", None),
    ],
)
def test_as_date_format_fallbacks(value: str, expected: dt.date | None) -> None:
    assert _as_date(value) == expected


def test_parse_row_rejects_short_and_bad_isin() -> None:
    assert _parse_row(["17-07-2026", "16:00:00", "IN0020260025"]) is None  # too few fields
    assert (
        _parse_row(["17-07-2026", "16:00:00", "NOTANISIN", "d", "1000", "100", "6", "NRML"]) is None
    )  # ISIN doesn't start with IN / wrong length


@respx.mock
def test_html_challenge_reprimes_then_succeeds(tmp_path: Path) -> None:
    respx.get("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )
    # First POST returns an Akamai challenge page; after a re-prime the retry returns the CSV.
    respx.post("https://www.ccilindia.com/g-sec-historical-trades").mock(
        side_effect=[
            httpx.Response(200, text="<!DOCTYPE html><html><body>Access Denied</body></html>"),
            httpx.Response(200, text=_CSV),
        ]
    )
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = CcilHistoricalTradesSource(client=ThrottledClient(settings.http), settings=settings)
    records = source.fetch_trades(dt.date(2026, 7, 17))
    assert {r.segment for r in records} == {"GSEC", "SDL", "TBILL"}


@respx.mock
def test_persistent_html_challenge_raises(tmp_path: Path) -> None:
    respx.get("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )
    respx.post("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text="<html><body>Access Denied</body></html>")
    )
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = CcilHistoricalTradesSource(client=ThrottledClient(settings.http), settings=settings)
    with pytest.raises(SourceError, match="non-CSV"):
        source.fetch_trades(dt.date(2026, 7, 17))


@respx.mock
def test_fetch_trades_end_to_end(tmp_path: Path) -> None:
    respx.get("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text="<html>ok</html>")
    )
    respx.post("https://www.ccilindia.com/g-sec-historical-trades").mock(
        return_value=httpx.Response(200, text=_CSV)
    )
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = CcilHistoricalTradesSource(client=ThrottledClient(settings.http), settings=settings)
    records = source.fetch_trades(dt.date(2026, 7, 17))
    assert {r.segment for r in records} == {"GSEC", "SDL", "TBILL"}
    assert (tmp_path / "raw" / "ccil" / "historical_trades_2026-07-17_2026-07-17.csv").exists()
