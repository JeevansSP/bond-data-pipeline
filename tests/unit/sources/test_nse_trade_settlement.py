"""Tests for the NSE trade & settlement (trade-level) source connector."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.sources.base import DataUnavailable
from bonds.sources.nse_trade_settlement import NseTradeSettlementSource

START = dt.date(2026, 7, 20)
END = dt.date(2026, 7, 26)

_HEADER = (
    '"Seller Deal Type ","Buyer Deal Type ","ISIN ","Listed/Unlisted Security ","Issuer Name ",'
    '"Coupon ","Issue description ","Price ","Yield ","Yield Type ","Outside Yield Range ",'
    '"Put/Call Date ","Trade Value ","Trade Date & Time ","Settlement Date ",'
    '"Reported trade/Trade executed on RFQ platform ","remarks","settlement status"'
)
_ROW = (
    '"Direct","Brokered","INE229U07186","Listed","NAMRA FINANCE LIMITED","10.9",'
    '"NAMRA FINANCE LIMITED 10.90 NCD 24AP28 FVRS10000","100.003","11.35","YTM","-",'
    '"24-04-2028","1.00","24-07-2026 09:44:00","27-07-2026","RFQ","","Pending"'
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _source(tmp_path: Path) -> NseTradeSettlementSource:
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    return NseTradeSettlementSource(client=ThrottledClient(settings.http), settings=settings)


def _csv(*rows: str) -> bytes:
    return ("﻿" + "\n".join([_HEADER, *rows])).encode()


def test_parse_maps_fields(tmp_path: Path) -> None:
    records, seen = _source(tmp_path).parse(_csv(_ROW))
    assert seen == 1
    (r,) = records
    assert r.isin == "INE229U07186"
    assert r.trade_date == dt.date(2026, 7, 24)  # from Trade Date & Time
    assert r.trade_time == dt.datetime(2026, 7, 24, 9, 44)
    assert r.seller_deal_type == "Direct"
    assert r.buyer_deal_type == "Brokered"
    assert r.coupon == pytest.approx(10.9)
    assert r.price == pytest.approx(100.003)
    assert r.trade_yield == pytest.approx(11.35)
    assert r.outside_yield_range is None  # "-" -> null
    assert r.put_call_date == dt.date(2028, 4, 24)
    assert r.trade_value_lakh == pytest.approx(1.0)
    assert r.settlement_date == dt.date(2026, 7, 27)
    assert r.venue == "RFQ"
    assert r.settlement_status == "Pending"


def test_parse_drops_rows_without_trade_time(tmp_path: Path) -> None:
    row = _ROW.replace('"24-07-2026 09:44:00"', '"-"')
    records, seen = _source(tmp_path).parse(_csv(row))
    assert records == [] and seen == 1


def test_parse_drops_bad_isin(tmp_path: Path) -> None:
    records, seen = _source(tmp_path).parse(_csv(_ROW.replace("INE229U07186", "XX")))
    assert records == [] and seen == 1


def test_download_rejects_oversize_window(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="7 days"):
        _source(tmp_path).download(START, START + dt.timedelta(days=7))


def test_fetch_trades_empty_window_is_data_unavailable(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "nse" / "trade_settlement" / f"{START}_{END}.csv"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(_csv())
    with pytest.raises(DataUnavailable):
        _source(tmp_path).fetch_trades(START, END)


def test_read_or_download_serves_lake_copy_without_network(tmp_path: Path) -> None:
    # No respx mock active: any HTTP call would raise.
    raw = tmp_path / "raw" / "nse" / "trade_settlement" / f"{START}_{END}.csv"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(_csv(_ROW))
    assert len(_source(tmp_path).fetch_trades(START, END)) == 1
