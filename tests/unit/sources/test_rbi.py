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


_INDEX_RESULTS = (
    b"<html><body><table>"
    b"<tr><td><a class='link2' href='FS_PressRelease.aspx?prid=63300&fn=2757'>"
    b"91-Day, 182-Day and 364-Day T-Bill Auction Result: Cut-off</a></td><td>x</td></tr>"
    b"<tr><td><a class='link2' href='FS_PressRelease.aspx?prid=63301&fn=2757'>"
    b"Result: Conversion/Switch Auction of Government of India Securities</a></td><td>x</td></tr>"
    b"</table></body></html>"
)


def test_parse_index_classifies_abbreviated_and_switch_titles() -> None:
    # RBI's result titles abbreviate "Treasury Bills" to "T-Bill", and a conversion/switch is a
    # G-Sec operation but not issuance; both fell through to "Other" until 2026-09-19.
    records = {r.prid: r for r in parse_index(_INDEX_RESULTS, source="rbi")}
    assert records["63300"].auction_type == "T-Bill"
    assert records["63301"].auction_type == "Switch"


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


def test_fetch_auctions_enriches_dates_and_lands_index(tmp_path: object) -> None:
    import httpx
    import respx

    from bonds.config import HttpSettings, Settings
    from bonds.http import ThrottledClient
    from bonds.sources.rbi import RbiSource

    with respx.mock:
        respx.get("https://www.rbi.org.in/scripts/FS_PressRelease.aspx?fn=2757").mock(
            return_value=httpx.Response(200, content=_INDEX)
        )
        for prid in ("63182", "63185"):
            respx.get(
                f"https://www.rbi.org.in/scripts/FS_PressRelease.aspx?prid={prid}&fn=2757"
            ).mock(return_value=httpx.Response(200, content=_DETAIL))

        settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))  # type: ignore[arg-type]
        source = RbiSource(client=ThrottledClient(settings.http), settings=settings)
        records = source.fetch_auctions(dt.date(2026, 7, 17))

    assert {r.prid for r in records} == {"63182", "63185"}
    assert all(r.auction_date == dt.date(2026, 7, 17) for r in records)
    assert source.metrics[0].rows_extracted == 2


def test_detail_date_survives_http_error() -> None:
    # A failing detail page (404/timeout) must NOT abort the whole auction ingest.
    from unittest.mock import MagicMock

    import httpx

    from bonds.models import RbiAuctionRecord
    from bonds.sources.rbi import RbiSource

    src = RbiSource.__new__(RbiSource)
    src._client = MagicMock()
    src._detail_cache = {}  # bypassing __init__ means the body cache must be set up by hand
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
