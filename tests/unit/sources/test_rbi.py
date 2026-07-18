"""Tests for the RBI auction-calendar connector (index + detail-date parsing)."""

from __future__ import annotations

import datetime as dt

import pytest

from bonds.sources.base import SourceError
from bonds.sources.rbi import parse_detail_date, parse_index

_INDEX = (
    b"<html><body><table>"
    b"<tr><td><a class='link2' href='FS_PressRelease.aspx?prid=63182&fn=2757'>"
    b"Auction of 91-Day, 182-Day and 364-Day Treasury Bills</a></td>"
    b"<td><a href='https://rbidocs.rbi.org.in/rdocs/PressRelease/PDFs/PRA.PDF'>"
    b"<img/></a> 377 kb</td></tr>"
    b"<tr><td><a class='link2' href='FS_PressRelease.aspx?prid=63185&fn=2757'>"
    b"Auction of State Government Securities</a></td>"
    b"<td><a href='https://rbidocs.rbi.org.in/rdocs/PressRelease/PDFs/PRB.PDF'><img/></a></td></tr>"
    b"<tr><td><a class='link2' href='FS_PressRelease.aspx?prid=63100&fn=2757'>"
    b"Premature redemption under Sovereign Gold Bond Scheme</a></td><td>x</td></tr>"
    b"</table></body></html>"
)

_DETAIL = (
    b"<html><body><table><tr><td>Date : Jul 17, 2026</td></tr>"
    b"<tr><td>6.03% GS 2029</td></tr></table></body></html>"
)


def test_parse_index_extracts_auctions_and_types() -> None:
    records = {r.prid: r for r in parse_index(_INDEX, source="rbi")}
    assert set(records) == {"63182", "63185"}  # SGB redemption is not an auction -> excluded
    assert records["63182"].auction_type == "T-Bill"
    assert records["63185"].auction_type == "SDL"
    pdf = records["63182"].pdf_url
    detail = records["63182"].detail_url
    assert pdf is not None and pdf.endswith("PRA.PDF")
    assert detail is not None and detail.endswith("FS_PressRelease.aspx?prid=63182&fn=2757")


def test_parse_index_raises_when_no_auctions() -> None:
    html = (
        b"<html><body><a href='FS_PressRelease.aspx?prid=1'>"
        b"Weekly Statistical Supplement</a></body></html>"
    )
    with pytest.raises(SourceError, match="auction"):
        parse_index(html, source="rbi")


def test_parse_detail_date() -> None:
    assert parse_detail_date(_DETAIL) == dt.date(2026, 7, 17)


def test_parse_detail_date_absent_returns_none() -> None:
    assert parse_detail_date(b"<html><body>no date here</body></html>") is None


def test_detail_date_survives_http_error() -> None:
    # A failing detail page (404/timeout) must NOT abort the whole auction ingest.
    from unittest.mock import MagicMock

    import httpx

    from bonds.models import RbiAuctionRecord
    from bonds.sources.rbi import RbiSource

    src = RbiSource.__new__(RbiSource)
    src._client = MagicMock()
    req = httpx.Request("GET", "https://www.rbi.org.in/x")
    src._client.get.side_effect = httpx.HTTPStatusError(
        "404", request=req, response=httpx.Response(404, request=req)
    )
    rec = RbiAuctionRecord(
        prid="1",
        title="Auction",
        auction_type="G-Sec",
        source="rbi",
        detail_url="https://www.rbi.org.in/scripts/FS_PressRelease.aspx?prid=1",
    )
    assert src._detail_date(rec) is None  # gracefully skipped, not raised
