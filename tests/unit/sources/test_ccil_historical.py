"""Tests for the CCIL historical-trades connector (AES, CSV aggregation, fetch)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.sources.ccil_historical import (
    CcilHistoricalTradesSource,
    aggregate_trades,
    encrypt_date,
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
