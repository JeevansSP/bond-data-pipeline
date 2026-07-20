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
    respx.get(URL, params={"page": "3", "size": "100"}).mock(
        return_value=httpx.Response(200, json=_page("IN8241O08017", total=3, has_next=False))
    )
    records = list(_source(tmp_path).iter_records(AS_OF))
    assert {r.isin for r in records} == {"INE002A07809", "IN8241O08017"}  # page 2 skipped


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
