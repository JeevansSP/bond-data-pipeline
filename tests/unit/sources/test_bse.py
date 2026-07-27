"""Tests for the BSE trade & settlement source connector."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.sources.base import DataUnavailable
from bonds.sources.bse import BseSource

DATE = dt.date(2026, 7, 24)

_ROW = {
    "DealType": "BROKERED",
    "ISIN": "INE413U08093",
    "IsListed": "LISTED",
    "Issuer_Name": "IIFL SAMASTA FINANCE LIMITED",
    "CouponRate": 11.0,
    "IssueDescription": "IIFL SAMASTA FINANCE LIMITED 11 LOA 18MY30 FVRS1LAC",
    "Trade_Price": 99.8854,
    "Trade_Yield": 11.0,
    "Yield_Type": "YTM",
    "Trade_Value": 25.0,
    "Trade_Time": "2026-07-24T16:55:23.607",
    "Settlement_Date": "27 Jul 2026",
    "RFQReported": "RFQ",
    "OrderStatus": "PENDING",
    "OutSideYieldRange": "",
    "YieldDate": "2030-05-18T00:00:00",
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _source(tmp_path: Path) -> BseSource:
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    return BseSource(client=ThrottledClient(settings.http), settings=settings)


def _payload(*rows: dict[str, object]) -> bytes:
    return json.dumps({"Table": list(rows)}).encode()


def test_parse_maps_fields(tmp_path: Path) -> None:
    records, seen = _source(tmp_path).parse(_payload(_ROW), date=DATE)
    assert seen == 1
    (r,) = records
    assert r.isin == "INE413U08093"
    assert r.trade_date == DATE  # derived from Trade_Time
    assert r.trade_time == dt.datetime(2026, 7, 24, 16, 55, 23, 607000)
    assert r.deal_type == "BROKERED"
    assert r.price == pytest.approx(99.8854)
    assert r.trade_yield == pytest.approx(11.0)
    assert r.trade_value_lakh == pytest.approx(25.0)
    assert r.settlement_date == dt.date(2026, 7, 27)
    assert r.venue == "RFQ"
    assert r.outside_yield_range is None  # empty string -> null
    assert r.put_call_date is None  # YTM trade: YieldDate is just maturity


def test_parse_ytc_yield_date_becomes_put_call_date(tmp_path: Path) -> None:
    row = dict(_ROW, Yield_Type="YTC")
    records, _ = _source(tmp_path).parse(_payload(row), date=DATE)
    assert records[0].put_call_date == dt.date(2030, 5, 18)


def test_parse_drops_bad_isin(tmp_path: Path) -> None:
    records, seen = _source(tmp_path).parse(_payload(dict(_ROW, ISIN="BAD")), date=DATE)
    assert records == [] and seen == 1


def test_fetch_trades_empty_day_is_data_unavailable(tmp_path: Path) -> None:
    src = _source(tmp_path)
    raw = tmp_path / "raw" / "bse" / "tradensettle" / f"{DATE.isoformat()}.json"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(_payload())
    with pytest.raises(DataUnavailable):
        src.fetch_trades(DATE)


@respx.mock
def test_download_lands_file(tmp_path: Path) -> None:
    respx.get("https://api.bseindia.com/BseIndiaAPI/api/rdbTradensettle/w").mock(
        return_value=httpx.Response(200, content=_payload(_ROW))
    )
    src = _source(tmp_path)
    records = src.fetch_trades(DATE)
    assert len(records) == 1
    landed = tmp_path / "raw" / "bse" / "tradensettle" / f"{DATE.isoformat()}.json"
    assert landed.exists()


def test_read_or_download_serves_lake_copy_without_network(tmp_path: Path) -> None:
    # No respx mock active: any HTTP call would raise.
    raw = tmp_path / "raw" / "bse" / "tradensettle" / f"{DATE.isoformat()}.json"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(_payload(_ROW))
    assert len(_source(tmp_path).fetch_trades(DATE)) == 1
