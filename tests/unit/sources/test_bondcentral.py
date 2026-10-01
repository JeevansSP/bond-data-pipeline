"""Tests for the BondCentral universe connector."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.models import InstrumentType
from bonds.sources.bondcentral import BondCentralSource

URL = "https://api.bondcentral.in/securities/"
AS_OF = dt.date(2026, 7, 18)

_PAGE_1: dict[str, Any] = {
    "data": [
        {
            "isin": "INE002A07809",
            "data": {
                "isin": "INE002A07809",
                "issuer": "RELIANCE INDUSTRIES LIMITED",
                "security_name": "RIL 7.79 NCD 10NV33",
                "coupon_rate": 7.79,
                "maturity_date": "2033-11-10 00:00:00",
                "face_value": "100000",
                "security_status": "ACTIVE",
                "secured_unsecured": "Secured",
                "ratings": [{"cra_rating": "AAA", "credit_rating_agency_name": "CARE"}],
            },
        },
        {"isin": "INBAD", "data": {"isin": "INBAD"}},  # invalid length -> skipped
    ],
    "pagination_info": {"total_pages": 2, "has_next": True},
}

_PAGE_2: dict[str, Any] = {
    "data": [
        {
            "isin": "IN8241O08017",
            "data": {
                "isin": "IN8241O08017",
                "issuer": "EDEL FINANCE COMPANY LIMITED",
                "security_name": "EDEL 9.25 NCD 04JN28",
                "coupon_rate": 9.25,
                "maturity_date": "2028-01-04 00:00:00",
                "face_value": "100000",
                "ratings": [{"cra_rating": None}],  # unrated -> None
            },
        }
    ],
    "pagination_info": {"total_pages": 2, "has_next": False},
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _source(tmp_path: Path) -> BondCentralSource:
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    return BondCentralSource(client=ThrottledClient(settings.http), settings=settings)


def _mock_pages() -> None:
    respx.get(URL, params={"page": "1", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_PAGE_1)
    )
    respx.get(URL, params={"page": "2", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_PAGE_2)
    )


@respx.mock
def test_iter_records_paginates_until_exhausted(tmp_path: Path) -> None:
    _mock_pages()
    records = list(_source(tmp_path).iter_records(AS_OF))
    assert {r.isin for r in records} == {"INE002A07809", "IN8241O08017"}
    assert all(r.instrument_type is InstrumentType.CORP for r in records)


def _page(isin: str, total: int, has_next: bool) -> dict[str, Any]:
    return {
        "data": [{"isin": isin, "data": {"isin": isin, "coupon_rate": 7.0}}],
        "pagination_info": {"total_pages": total, "has_next": has_next},
    }


@respx.mock
def test_iter_records_skips_persistently_failing_page(tmp_path: Path) -> None:
    # BondCentral 500s a broken middle page; the pull must skip it and keep going, not abort.
    respx.get(URL, params={"page": "1", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("INE002A07809", total=3, has_next=True))
    )
    respx.get(URL, params={"page": "2", "size": "100"}).mock(return_value=httpx.Response(500))
    # ...and every slice and single record of it fails too, so the window is written off.
    respx.get(URL, params={"size": "10"}).mock(return_value=httpx.Response(500))
    respx.get(URL, params={"size": "1"}).mock(return_value=httpx.Response(500))
    respx.get(URL, params={"page": "3", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("IN8241O08017", total=3, has_next=False))
    )
    records = list(_source(tmp_path).iter_records(AS_OF))
    assert {r.isin for r in records} == {"INE002A07809", "IN8241O08017"}  # page 2 skipped


def _subpage(isin: str, *, total_records: int, has_next: bool, size: int = 10) -> dict[str, Any]:
    return {
        "data": [{"isin": isin, "data": {"isin": isin, "coupon_rate": 7.0}}],
        "pagination_info": {
            "total_records": total_records,
            "total_pages": -(-total_records // size),
            "has_next": has_next,
        },
    }


@respx.mock
def test_failing_page_is_recovered_as_subpages(tmp_path: Path) -> None:
    # The windows BondCentral 500s at size=100 mostly serve at size=10, and a slice that still
    # fails is read record by record: page 2 is re-read as slices 11..20; slice 11 fails — even
    # though it is the FIRST slice, page 1 just succeeded so the feed is known to be up — and is
    # re-read as records 101..110, of which only 103 (the unserialisable one) is lost. has_next
    # comes from the last slice, so the crawl still continues to page 3.
    respx.get(URL, params={"page": "1", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("INE002A07809", total=3, has_next=True))
    )
    respx.get(URL, params={"page": "2", "size": "100"}).mock(return_value=httpx.Response(500))
    for sub in range(11, 21):
        respx.get(URL, params={"page": str(sub), "size": "10"}).mock(
            return_value=httpx.Response(500)
            if sub == 11
            else httpx.Response(
                200, json=_subpage(f"INE{sub:03d}A07001", total_records=300, has_next=True)
            )
        )
    for rec in range(101, 111):
        respx.get(URL, params={"page": str(rec), "size": "1"}).mock(
            return_value=httpx.Response(500)
            if rec == 103
            else httpx.Response(
                200,
                json=_subpage(f"INE{rec:03d}A07002", total_records=300, has_next=True, size=1),
            )
        )
    respx.get(URL, params={"page": "3", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("IN8241O08017", total=3, has_next=False))
    )
    source = _source(tmp_path)
    isins = {r.isin for r in source.iter_records(AS_OF)}
    assert {"INE002A07809", "IN8241O08017"} <= isins
    assert {f"INE{sub:03d}A07001" for sub in range(12, 21)} <= isins  # the nine good slices
    assert {f"INE{rec:03d}A07002" for rec in range(101, 111) if rec != 103} <= isins
    assert "INE103A07002" not in isins  # the one record that cannot be served at any size
    base = tmp_path / "raw" / "bondcentral" / AS_OF.isoformat()
    assert (base / "page_0012_size010.json").exists()  # slices land under their own name
    assert (base / "page_0104_size001.json").exists()  # so do single records
    assert not (base / "page_0002.json").exists()  # the failed full page never landed
    assert source.metrics[0].rows_parsed == 1 + 9 + 9 + 1


@respx.mock
def test_second_consecutive_failing_page_bails_on_its_first_slice(tmp_path: Path) -> None:
    # Page 2 fails and every slice fails (feed down). Page 3 then fails too: with the previous
    # page already failed, the first failing slice must end the attempt — an outage costs one
    # extra request per page, not ten — so slices 22..30 are never requested.
    respx.get(URL, params={"page": "1", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("INE002A07809", total=4, has_next=True))
    )
    respx.get(URL, params={"page": "2", "size": "100"}).mock(return_value=httpx.Response(500))
    respx.get(URL, params={"page": "3", "size": "100"}).mock(return_value=httpx.Response(500))
    respx.get(URL, params={"page": "4", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("IN8241O08017", total=4, has_next=False))
    )
    slices = {
        sub: respx.get(URL, params={"page": str(sub), "size": "10"}).mock(
            return_value=httpx.Response(500)
        )
        for sub in range(11, 31)
    }
    respx.get(URL, params={"size": "1"}).mock(return_value=httpx.Response(500))
    isins = {r.isin for r in _source(tmp_path).iter_records(AS_OF)}
    assert isins == {"INE002A07809", "IN8241O08017"}
    assert all(slices[sub].called for sub in range(11, 21))  # page 2: feed was alive, all tried
    assert slices[21].called and not any(slices[sub].called for sub in range(22, 31))


@respx.mock
def test_recovered_page_with_lost_tail_slice_keeps_crawling(tmp_path: Path) -> None:
    # When the LAST slice of a recovered page is lost, has_next is unknown; the record count
    # (300 / 100 = 3 pages) must decide that page 3 exists rather than ending the crawl early.
    respx.get(URL, params={"page": "1", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("INE002A07809", total=3, has_next=True))
    )
    respx.get(URL, params={"page": "2", "size": "100"}).mock(return_value=httpx.Response(500))
    for sub in range(11, 20):
        respx.get(URL, params={"page": str(sub), "size": "10"}).mock(
            return_value=httpx.Response(
                200, json=_subpage(f"INE{sub:03d}A07001", total_records=300, has_next=True)
            )
        )
    respx.get(URL, params={"page": "20", "size": "10"}).mock(return_value=httpx.Response(500))
    respx.get(URL, params={"size": "1"}).mock(return_value=httpx.Response(500))  # records too
    respx.get(URL, params={"page": "3", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("IN8241O08017", total=3, has_next=False))
    )
    isins = {r.isin for r in _source(tmp_path).iter_records(AS_OF)}
    assert "IN8241O08017" in isins


@respx.mock
def test_iter_records_aborts_when_too_many_pages_fail(tmp_path: Path) -> None:
    from bonds.sources.base import SourceError

    respx.get(URL).mock(return_value=httpx.Response(500))  # every page fails
    with pytest.raises(SourceError, match="pages failed"):
        list(_source(tmp_path).iter_records(AS_OF))


@respx.mock
def test_parsing_maps_fields_and_rating(tmp_path: Path) -> None:
    _mock_pages()
    by_isin = {r.isin: r for r in _source(tmp_path).iter_records(AS_OF)}

    ril = by_isin["INE002A07809"]
    assert ril.issuer == "RELIANCE INDUSTRIES LIMITED"
    assert ril.description == "RIL 7.79 NCD 10NV33"
    assert ril.coupon == pytest.approx(7.79)
    assert ril.maturity_date == dt.date(2033, 11, 10)
    assert ril.face_value == pytest.approx(100000.0)
    assert ril.attributes["credit_rating"] == "AAA"

    edel = by_isin["IN8241O08017"]
    assert edel.attributes["credit_rating"] is None  # unrated


@respx.mock
def test_max_pages_caps_fetch(tmp_path: Path) -> None:
    _mock_pages()
    records = list(_source(tmp_path).iter_records(AS_OF, max_pages=1))
    assert {r.isin for r in records} == {"INE002A07809"}  # page 2 never fetched


@respx.mock
def test_raw_pages_are_landed(tmp_path: Path) -> None:
    _mock_pages()
    list(_source(tmp_path).iter_records(AS_OF))
    base = tmp_path / "raw" / "bondcentral" / AS_OF.isoformat()
    assert (base / "page_0001.json").exists()
    assert (base / "page_0002.json").exists()


@respx.mock
def test_collects_etl_metrics(tmp_path: Path) -> None:
    _mock_pages()
    source = _source(tmp_path)
    list(source.iter_records(AS_OF))
    assert len(source.metrics) == 1
    metric = source.metrics[0]
    assert metric.artifact == "universe"
    assert metric.rows_parsed == 2  # two valid ISINs across the two pages
    assert metric.rows_dropped == 1  # the invalid-length ISIN row
    assert metric.bytes_downloaded > 0


@respx.mock
def test_fetch_reference_returns_matching_record(tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=_PAGE_1))
    record = _source(tmp_path).fetch_reference("INE002A07809")
    assert record is not None
    assert record.isin == "INE002A07809"
    assert record.coupon == pytest.approx(7.79)


@respx.mock
def test_fetch_reference_rejects_mismatched_isin(tmp_path: Path) -> None:
    # If the API ever ignores the isin filter, enrichment must not write another security's
    # reference data onto the requested row.
    respx.get(URL).mock(return_value=httpx.Response(200, json=_PAGE_1))
    assert _source(tmp_path).fetch_reference("INE999X99999") is None


@respx.mock
def test_fetch_reference_raises_source_error_on_non_json(tmp_path: Path) -> None:
    from bonds.sources.base import SourceError

    respx.get(URL).mock(return_value=httpx.Response(200, content=b"<html>challenge</html>"))
    with pytest.raises(SourceError, match="non-JSON"):
        _source(tmp_path).fetch_reference("INE002A07809")


@respx.mock
def test_fetch_reference_not_covered_returns_none(tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"data": []}))
    assert _source(tmp_path).fetch_reference("INE002A07809") is None


@respx.mock
def test_iter_records_skips_non_json_page_and_continues(tmp_path: Path) -> None:
    # A 200 with a non-JSON body (proxy error page) must be skipped like an HTTP error,
    # not abort the whole snapshot.
    respx.get(URL, params={"page": "1", "size": "100"}).mock(
        return_value=httpx.Response(200, content=b"<html>edge error</html>")
    )
    respx.get(URL, params={"page": "1", "size": "10"}).mock(return_value=httpx.Response(500))
    respx.get(URL, params={"page": "2", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_PAGE_2)
    )
    records = list(_source(tmp_path).iter_records(AS_OF, max_pages=2))
    assert {r.isin for r in records} == {"IN8241O08017"}


@respx.mock
def test_tail_failures_without_total_pages_keep_the_snapshot(tmp_path: Path) -> None:
    # If the API ever stops sending total_pages and the last page errors, the skip loop must
    # not probe ~30 phantom pages and discard the nearly-complete snapshot: after one good
    # page, 3 consecutive failures with no known total means end-of-feed.
    page1 = dict(_PAGE_1, pagination_info={"has_next": True})  # no total_pages
    respx.get(URL, params={"page": "1", "size": "100"}).mock(
        return_value=httpx.Response(200, json=page1)
    )
    for p in range(2, 6):
        respx.get(URL, params={"page": str(p), "size": "100"}).mock(
            return_value=httpx.Response(500)
        )
    respx.get(URL, params={"size": "10"}).mock(return_value=httpx.Response(500))  # no sub-pages
    respx.get(URL, params={"size": "1"}).mock(return_value=httpx.Response(500))
    records = list(_source(tmp_path).iter_records(AS_OF))
    assert {r.isin for r in records} == {"INE002A07809"}  # page 1 kept, no SourceError
