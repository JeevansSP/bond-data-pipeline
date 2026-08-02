"""Tests for the NSE corporate bond report source connector."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.models import InstrumentType
from bonds.sources.nse_bond_report import NseBondReportSource

DATE = dt.date(2026, 7, 24)

_HEADER = (
    "Sectype,Security,Issue Name,Issue Desc,Issuer,Face Value,Credit Rating,Issue Date,"
    "Maturity Date,Record Date,Step up Coupons,Coupon Frequency,Next Coupon Date,"
    "Day Count Convention,Floating Benchmark,Spread over the Benchmark,Last Trade Date,"
    "Last trade price,Last Trade Value (Rs.in Lakhs),Last Trade Yield,Weighted Average Price,"
    "Weighted Average Yield,Traded Value (Rs. In Crores),ISIN,Status"
)
_PREAMBLE = (
    ",,,National Stock Exchange Of India Limited,,,,,,,,,,,,,,,,,,,,,\n"
    ",,,Negotiated Trade Reporting Platform,,,,,,,,,,,,,,,,,,,,,\n"
    "Details report for :- 24-Jul-2026,,,,,,,,,,,,,,,,,,,,,,,,\n"
)
_ROW = (
    "AT,AXBK32,7.88%,Axis 7.88% 2032 Sr 30 Tier 2,UTI BANK LIMITED,100.00,CRISIL AAA,"
    "13-Dec-2022,13-Dec-2032,,,Yearly,13-Dec-2026,ACTUALby365,,,,0.0000,,,,,,"
    "INE238A08484,Listed"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _source(tmp_path: Path) -> NseBondReportSource:
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    return NseBondReportSource(client=ThrottledClient(settings.http), settings=settings)


def _land(tmp_path: Path, body: str) -> None:
    raw = tmp_path / "raw" / "nse" / "corporate_bond_report" / f"{DATE.isoformat()}.csv"
    raw.parent.mkdir(parents=True)
    raw.write_text(body)


def test_iter_records_maps_fields(tmp_path: Path) -> None:
    _land(tmp_path, _PREAMBLE + _HEADER + "\n" + _ROW + "\n")
    (r,) = list(_source(tmp_path).iter_records(DATE))
    assert r.isin == "INE238A08484"
    assert r.instrument_type is InstrumentType.CORP
    assert r.coupon == pytest.approx(7.88)  # parsed from "7.88%"
    assert r.interest_type == "FIXED"  # validator canonicalizes the source's "Fixed"
    assert r.maturity_date == dt.date(2032, 12, 13)
    assert r.face_value == pytest.approx(100.0)
    assert r.attributes["day_count_convention"] == "ACTUALby365"
    assert r.attributes["coupon_frequency"] == "Yearly"
    assert r.attributes["next_coupon_date"] == "2026-12-13"  # ISO re-serialized
    assert r.attributes["issuance_date"] == "2022-12-13"
    assert r.attributes["credit_rating_nse"] == "CRISIL AAA"
    assert r.attributes["listing_status"] == "Listed"


def test_iter_records_handles_repeated_headers_and_junk_rows(tmp_path: Path) -> None:
    # The real file repeats its section header and interleaves blank/branding rows.
    _land(tmp_path, _PREAMBLE + _HEADER + "\n" + _ROW + "\n,,,,\n" + _HEADER + "\n" + _ROW + "\n")
    records = list(_source(tmp_path).iter_records(DATE))
    assert [r.isin for r in records] == ["INE238A08484", "INE238A08484"]


def test_floating_benchmark_sets_interest_type(tmp_path: Path) -> None:
    row = _ROW.replace("ACTUALby365,,,", "ACTUALby365,MIBOR,0.50,")
    _land(tmp_path, _PREAMBLE + _HEADER + "\n" + row + "\n")
    (r,) = list(_source(tmp_path).iter_records(DATE))
    assert r.interest_type == "FLOATING"  # validator canonicalizes the source's "Floating"
    assert r.attributes["floating_benchmark"] == "MIBOR"
    assert r.attributes["benchmark_spread"] == "0.50"


def test_commercial_paper_issue_name_is_not_a_coupon(tmp_path: Path) -> None:
    # NSE reuses "Issue Name" for a CP's DDMMYY maturity, sometimes with a stray '%'
    # ("70525%") that would otherwise parse as a 70,525% coupon.
    cp = (
        "CP,ABCL,70525%,ABCL CP 07/05/25 Sr 162,ADITYA BIRLA FINANCE LTD,100.00,,"
        "13-Nov-2024,07-May-2025,,,,,ACTUALby365,,,,0.0000,,,,,,INE860H144I3,Listed"
    )
    _land(tmp_path, _PREAMBLE + _HEADER + "\n" + cp + "\n")
    (r,) = list(_source(tmp_path).iter_records(DATE))
    assert r.coupon is None
    assert r.interest_type is None


def test_implausible_coupon_is_rejected(tmp_path: Path) -> None:
    # A non-CP row whose rate field is mis-encoded must yield no coupon, not a junk one.
    _land(tmp_path, _PREAMBLE + _HEADER + "\n" + _ROW.replace("7.88%", "70525%") + "\n")
    (r,) = list(_source(tmp_path).iter_records(DATE))
    assert r.coupon is None
