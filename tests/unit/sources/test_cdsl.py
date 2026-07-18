"""Tests for the CDSL issuer/outstanding snapshot connector."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.models import InstrumentType
from bonds.sources.base import SourceError
from bonds.sources.cdsl import CdslSource, parse_snapshot

REPORT_DATE = dt.date(2025, 9, 30)

_HEADER = (
    "<tr><th>Sr. No.</th><th>Name of the Issuer</th><th>ISIN</th>"
    "<th>Issuance Date</th><th>Maturity Date</th><th>Coupon Rate</th>"
    "<th>Payment Frequency</th><th>Embedded option</th>"
    "<th>Amount issued</th><th>Amount outstanding</th></tr>"
)
_HTML = (
    "<html><body><table>"
    + _HEADER
    + "<tr><td>1</td><td>RELIANCE INDUSTRIES LIMITED</td><td>INE002A07809</td>"
    "<td>10-Nov-23</td><td>10-Nov-33</td><td>7.79</td><td>Once a year</td>"
    "<td>Put: N/A</td><td>2,000.00</td><td>1500.50</td></tr>"
    "<tr><td>2</td><td>EDEL FINANCE COMPANY LIMITED</td><td>IN8241O08017</td>"
    "<td>09-Jan-18</td><td>04-Jan-28</td><td>NA</td><td>Cumulative</td>"
    "<td>-</td><td>10.00</td><td>0.00</td></tr>"
    "<tr><td>junk</td><td>too few columns</td></tr>"
    "</table></body></html>"
).encode()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_parse_snapshot_extracts_records() -> None:
    records = {r.isin: r for r in parse_snapshot(_HTML)}
    assert set(records) == {"INE002A07809", "IN8241O08017"}

    ril = records["INE002A07809"]
    assert ril.instrument_type is InstrumentType.CORP
    assert ril.issuer == "RELIANCE INDUSTRIES LIMITED"
    assert ril.coupon == pytest.approx(7.79)
    assert ril.maturity_date == dt.date(2033, 11, 10)
    assert ril.attributes["amount_outstanding_cr"] == "1500.50"
    assert ril.attributes["amount_issued_cr"] == "2,000.00"


def test_parse_snapshot_handles_na_coupon() -> None:
    records = {r.isin: r for r in parse_snapshot(_HTML)}
    assert records["IN8241O08017"].coupon is None  # "NA"


def test_parse_snapshot_handles_both_date_formats() -> None:
    # CDSL mixes "dd-Mon-YY" and "dd-mm-YYYY" maturities in the same report.
    row = (
        "<tr><td>3</td><td>ACME LTD</td><td>INE123A07AB4</td>"
        "<td>21-08-2021</td><td>21-08-2026</td><td>8.0</td><td>Annual</td>"
        "<td>-</td><td>50.00</td><td>50.00</td></tr>"
    )
    html = f"<html><body><table>{_HEADER}{row}</table></body></html>".encode()
    record = next(iter(parse_snapshot(html)))
    assert record.maturity_date == dt.date(2026, 8, 21)


def test_parse_snapshot_raises_when_no_isin_rows() -> None:
    html = f"<html><body><table>{_HEADER}<tr><td>x</td></tr></table></body></html>".encode()
    with pytest.raises(SourceError, match="ISIN"):
        list(parse_snapshot(html))


@respx.mock
def test_fetch_snapshot_lands_file(tmp_path: Path) -> None:
    respx.get("https://www.cdslindia.com/CorporateBond/IssuerReportDetails.aspx").mock(
        return_value=httpx.Response(200, content=_HTML)
    )
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = CdslSource(client=ThrottledClient(settings.http), settings=settings)
    records = list(source.iter_records(REPORT_DATE))
    assert len(records) == 2
    assert (tmp_path / "raw" / "cdsl" / "2025-09-30.html").exists()
