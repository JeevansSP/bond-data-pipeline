"""Tests for the NSE corporate-bond trade connector."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.sources.nse import NseSource, _as_float, _parse_timestamp, _to_record

_ROW = {
    "descriptor": "RELIANCE 7.79 NCD 10NV33\r",
    "isin": "INE002A07809",
    "ltp": 99.88,
    "lty": 7.52,
    "noOfTrades": 4,
    "tradeValue": 23800,
    "wap": 99.88,
    "way": 7.52,
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_parse_timestamp() -> None:
    assert _parse_timestamp("17-Jul-2026 17:09") == dt.date(2026, 7, 17)
    assert _parse_timestamp("") is None
    assert _parse_timestamp(None) is None


def test_to_record_maps_fields_and_strips_descriptor() -> None:
    rec = _to_record(_ROW, "otctrades_listed", dt.date(2026, 7, 17))
    assert rec is not None
    assert rec.isin == "INE002A07809"
    assert rec.descriptor == "RELIANCE 7.79 NCD 10NV33"  # trailing \r stripped
    assert rec.ltp == pytest.approx(99.88)
    assert rec.no_of_trades == 4
    assert rec.segment == "otctrades_listed"


def test_to_record_skips_bad_isin() -> None:
    assert _to_record({"isin": "BAD"}, "otctrades_listed", dt.date(2026, 7, 17)) is None


def test_as_float_handles_indian_grouped_numbers() -> None:
    assert _as_float("1,23,456.50") == pytest.approx(123456.50)  # was silently nulled before
    assert _as_float("100") == pytest.approx(100.0)
    assert _as_float("") is None


@respx.mock
def test_fetch_trades_primes_cookies_and_parses_segments(tmp_path: Path) -> None:
    respx.get(
        "https://www.nseindia.com/market-data/debt-market-reporting-corporate-bonds-traded-on-exchange"
    ).mock(return_value=httpx.Response(200, text="<html>ok</html>"))
    # listed OTC has data; the other three segments are empty
    respx.get(
        "https://www.nseindia.com/api/liveCorp-bonds", params={"index": "otctrades_listed"}
    ).mock(
        return_value=httpx.Response(200, json={"data": [_ROW], "timestamp": "17-Jul-2026 17:09"})
    )
    respx.get("https://www.nseindia.com/api/liveCorp-bonds").mock(
        return_value=httpx.Response(200, json={"data": [], "timestamp": ""})
    )

    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = NseSource(client=ThrottledClient(settings.http), settings=settings)
    records = source.fetch_trades(dt.date(2026, 7, 18))

    assert len(records) == 1
    assert records[0].trade_date == dt.date(2026, 7, 17)  # from the envelope timestamp
    assert (tmp_path / "raw" / "nse" / "2026-07-18" / "otctrades_listed.json").exists()
