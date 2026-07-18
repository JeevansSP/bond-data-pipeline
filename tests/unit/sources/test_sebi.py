"""Tests for the SEBI public-issue connector."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.sources.base import SourceError
from bonds.sources.sebi import SebiSource, parse_public_issues

_HEADER = (
    "<tr><th>S.No</th><th>Name of company</th><th>Issue opened on</th>"
    "<th>Issue closed on</th><th>Base issue size Rs Cr</th><th>Final Issue size Rs Cr*</th></tr>"
)
_HTML = (
    "<html><body>"
    "<table>" + _HEADER + "<tr><td>1</td><td>Tata Capital Limited</td>"
    "<td>02-Feb-09</td><td>24-Feb-09</td><td>500.00</td><td>1500.00</td></tr>"
    "<tr><td>Total</td><td>1500.00</td></tr>"  # summary row -> skipped
    "</table>"
    "<table>" + _HEADER + "<tr><td>1</td><td>Muthoot Fincorp Limited</td>"
    "<td>10-Mar-26</td><td>23-Mar-26</td><td>100.00</td><td>360.00</td></tr>"
    "</table>"
    "</body></html>"
).encode()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_parse_public_issues_across_fy_tables() -> None:
    issues = parse_public_issues(_HTML)
    assert len(issues) == 2
    tata = next(i for i in issues if i.company == "Tata Capital Limited")
    assert tata.issue_open == dt.date(2009, 2, 2)
    assert tata.issue_close == dt.date(2009, 2, 24)
    assert tata.base_size_cr == pytest.approx(500.0)
    assert tata.final_size_cr == pytest.approx(1500.0)
    assert tata.financial_year == "2008-09"  # Feb 2009 -> FY 2008-09


def test_parse_skips_total_rows() -> None:
    assert all(i.company.lower() != "total" for i in parse_public_issues(_HTML))


def test_parse_raises_when_no_rows() -> None:
    with pytest.raises(SourceError, match="public-issue"):
        parse_public_issues(b"<html><body><table><tr><th>x</th></tr></table></body></html>")


@respx.mock
def test_fetch_lands_file(tmp_path: Path) -> None:
    respx.get("https://www.sebi.gov.in/statistics/corporate-bonds/publicissuedata.html").mock(
        return_value=httpx.Response(200, content=_HTML)
    )
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    source = SebiSource(client=ThrottledClient(settings.http), settings=settings)
    issues = source.fetch_public_issues(dt.date(2026, 7, 18))
    assert len(issues) == 2
    assert (tmp_path / "raw" / "sebi" / "public_issues_2026-07-18.html").exists()
