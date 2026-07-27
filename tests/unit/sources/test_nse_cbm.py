"""Tests for the NSE CBM daily archive source connector."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.sources.base import DataUnavailable
from bonds.sources.nse_cbm import NseCbmDailySource

DATE = dt.date(2026, 7, 24)

_HEADER = (
    "Trade Date,ISIN,Last Trade Price (in Rs.),Last Trade Value (Rs. in lacs),"
    "Total Trade Value (Rs. in lacs),Last Trade Yield (YTM) (Annualized) (%),"
    "Weighted Average Price  (Rs.),Weighted Average Yield (YTM) (%)"
)
_ROW = "24-Jul-2026,INE296A07SV1 ,99.8000,5000.00,15000.00,7.8378,99.8067,7.8366"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _source(tmp_path: Path) -> NseCbmDailySource:
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    return NseCbmDailySource(client=ThrottledClient(settings.http), settings=settings)


def _land(tmp_path: Path, body: str) -> None:
    raw = tmp_path / "raw" / "nse" / "cbm_trd" / f"{DATE.isoformat()}.csv"
    raw.parent.mkdir(parents=True)
    raw.write_text(body)


def test_fetch_trades_maps_fields(tmp_path: Path) -> None:
    _land(tmp_path, _HEADER + "\n" + _ROW + "\n")
    (r,) = _source(tmp_path).fetch_trades(DATE)
    assert r.isin == "INE296A07SV1"  # trailing space stripped
    assert r.trade_date == DATE
    assert r.segment == "cbm"
    assert r.source == "nse_cbm"
    assert r.ltp == pytest.approx(99.8)
    assert r.lty == pytest.approx(7.8378)
    assert r.trade_value == pytest.approx(15000.0)  # total, not last
    assert r.wap == pytest.approx(99.8067)
    assert r.way == pytest.approx(7.8366)


def test_parse_drops_bad_rows(tmp_path: Path) -> None:
    body = _HEADER + "\nbad-date,NOTANISIN,1,2,3,4,5,6\n"
    records, seen = _source(tmp_path).parse(body.encode())
    assert records == [] and seen == 1


def test_fetch_trades_empty_file_is_data_unavailable(tmp_path: Path) -> None:
    _land(tmp_path, _HEADER + "\n")
    with pytest.raises(DataUnavailable):
        _source(tmp_path).fetch_trades(DATE)
