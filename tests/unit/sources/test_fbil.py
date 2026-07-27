"""Tests for the FBIL source connector."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
import respx

from bonds.config import HttpSettings, Settings
from bonds.http import ThrottledClient
from bonds.models import InstrumentType
from bonds.sources.base import DataUnavailable, SourceError
from bonds.sources.fbil import FbilSource

DATE = dt.date(2026, 7, 10)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _source(tmp_path: Path) -> FbilSource:
    settings = Settings(data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0))
    return FbilSource(client=ThrottledClient(settings.http), settings=settings)


def test_parse_extracts_records(tmp_path: Path, fbil_gsec_workbook: bytes) -> None:
    records = _source(tmp_path).parse(fbil_gsec_workbook, date=DATE, instrument=InstrumentType.GSEC)
    assert len(records) == 2
    first = records[0]
    assert first.isin == "IN0020160035"
    assert first.coupon == pytest.approx(6.97)
    assert first.maturity_date == dt.date(2026, 9, 6)
    assert first.price == pytest.approx(100.2374)
    assert first.ytm == pytest.approx(5.265)
    assert first.instrument_type is InstrumentType.GSEC
    assert first.quote_date == DATE


def test_parse_ignores_non_isin_rows(tmp_path: Path, fbil_gsec_workbook: bytes) -> None:
    records = _source(tmp_path).parse(fbil_gsec_workbook, date=DATE, instrument=InstrumentType.GSEC)
    assert all(r.isin.startswith("IN") and len(r.isin) == 12 for r in records)


def test_parse_raises_without_header(tmp_path: Path) -> None:
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["no", "isin", "header", "here"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(SourceError, match="ISIN"):
        _source(tmp_path).parse(buf.getvalue(), date=DATE, instrument=InstrumentType.GSEC)


def test_parse_finds_data_sheet_when_not_active(tmp_path: Path) -> None:
    # FBIL sometimes saves the file with a non-data tab (e.g. "Note on FRB & IIB") active while the
    # real G-Sec sheet sits alongside; parse must search all sheets, not trust workbook.active.
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    data = wb.active
    data.title = "G-Sec"
    data.append(["ISIN", "Coupon", "Maturity(dd-mmm-yyyy)", "Price(Rs)", "YTM% p.a. (Semi-Annual)"])
    data.append(["IN0020160035", 6.97, dt.datetime(2026, 9, 6), 100.24, 5.27])
    note = wb.create_sheet("Note on FRB & IIB")
    note.append([None, "Note on FRB & IIB", "23-Feb-2023"])
    wb.active = wb.sheetnames.index("Note on FRB & IIB")  # non-data sheet is active
    buf = io.BytesIO()
    wb.save(buf)

    records = _source(tmp_path).parse(buf.getvalue(), date=DATE, instrument=InstrumentType.GSEC)
    assert [r.isin for r in records] == ["IN0020160035"]


def test_parse_non_xlsx_body_is_data_unavailable(tmp_path: Path) -> None:
    # FBIL serves an HTML page (HTTP 200) for dates outside its published range; that must be
    # treated as no-data (SKIPPED), not crash a backfill with BadZipFile.
    html = b"<!DOCTYPE html><html><body>No data</body></html>"
    with pytest.raises(DataUnavailable, match="non-xlsx"):
        _source(tmp_path).parse(html, date=DATE, instrument=InstrumentType.GSEC)


@respx.mock
def test_download_lands_file_and_returns_bytes(tmp_path: Path, fbil_gsec_workbook: bytes) -> None:
    respx.get("https://www.fbil.org.in/wasdm/gsec/downloadPublished").mock(
        return_value=httpx.Response(200, content=fbil_gsec_workbook)
    )
    src = _source(tmp_path)
    content = src.download("gsec", DATE)
    assert content == fbil_gsec_workbook
    landed = tmp_path / "raw" / "fbil" / "gsec" / "2026-07-10.xlsx"
    assert landed.read_bytes() == fbil_gsec_workbook


@respx.mock
def test_download_raises_data_unavailable_on_500(tmp_path: Path) -> None:
    respx.get("https://www.fbil.org.in/wasdm/gsec/downloadPublished").mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(DataUnavailable):
        _source(tmp_path).download("gsec", DATE)


@respx.mock
def test_download_does_not_retry_holiday_500(tmp_path: Path) -> None:
    # 500 = non-publishing day; it must fail fast, not burn the retry budget.
    route = respx.get("https://www.fbil.org.in/wasdm/gsec/downloadPublished").mock(
        return_value=httpx.Response(500)
    )
    settings = Settings(
        data_root=tmp_path, http=HttpSettings(min_interval_seconds=0.0, max_retries=4)
    )
    source = FbilSource(client=ThrottledClient(settings.http), settings=settings)
    with pytest.raises(DataUnavailable):
        source.download("gsec", DATE)
    assert route.call_count == 1  # not 4


@respx.mock
def test_fetch_valuations_end_to_end(tmp_path: Path, fbil_gsec_workbook: bytes) -> None:
    respx.get("https://www.fbil.org.in/wasdm/gsec/downloadPublished").mock(
        return_value=httpx.Response(200, content=fbil_gsec_workbook)
    )
    records = _source(tmp_path).fetch_valuations("gsec", DATE)
    assert {r.isin for r in records} == {"IN0020160035", "IN0020010081"}


def test_fetch_valuations_rejects_unknown_product(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _source(tmp_path).fetch_valuations("equities", DATE)


def test_parse_includes_special_sheet(tmp_path: Path, fbil_gsec_workbook_full: bytes) -> None:
    # GoI special securities (oil/FCI/recap bonds) live on a second ISIN-headed sheet;
    # the parser must read every data sheet, not just the first.
    records = _source(tmp_path).parse(
        fbil_gsec_workbook_full, date=DATE, instrument=InstrumentType.GSEC
    )
    assert {r.isin for r in records} == {"IN0020160035", "IN0020060029"}
    special = next(r for r in records if r.isin == "IN0020060029")
    assert special.description == "08.23 GOI FCI 2027"
    assert special.price == pytest.approx(101.2)


def test_parse_classifies_by_sheet_title(tmp_path: Path) -> None:
    # Pre-2023 gsec workbooks bundle SDL (and hyphenated "Par-Yield") sheets in one file;
    # each data sheet's title must pick its instrument, not the product's.
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    gsec = wb.active
    gsec.title = "G-Sec"
    gsec.append(["ISIN", "Coupon", "Maturity", "Price(Rs)", "YTM% p.a."])
    gsec.append(["IN0020160035", 6.97, dt.datetime(2026, 9, 6), 100.24, 5.27])
    sdl = wb.create_sheet("SDL")
    sdl.append(["ISIN", "Description", "Coupon", "Maturity", "Price(Rs)", "YTM% p.a."])
    sdl.append(["IN1520160061", "07.83 GJ SDL 2026", 7.83, dt.datetime(2026, 7, 13), 100.0, 5.4])
    par = wb.create_sheet("Par-Yield")  # old hyphenated title
    par.append(["Tenor (Year)", "YTM% p.a.(Semi-Annual)", "YTM % p.a.(Annualized)"])
    par.append([0.25, 5.31, 5.39])
    buf = io.BytesIO()
    wb.save(buf)

    records = _source(tmp_path).parse(buf.getvalue(), date=DATE, instrument=InstrumentType.GSEC)
    by_isin = {r.isin: r.instrument_type for r in records}
    assert by_isin == {
        "IN0020160035": InstrumentType.GSEC,
        "IN1520160061": InstrumentType.SDL,
    }
    points = _source(tmp_path).parse_curve(buf.getvalue(), product="gsec", date=DATE)
    assert [p.tenor_years for p in points] == [0.25]


def test_parse_strips_yield_header_variant(tmp_path: Path, fbil_strips_workbook: bytes) -> None:
    # The STRIPS sheet titles its yield column "Yield%…" rather than "YTM…".
    records = _source(tmp_path).parse(
        fbil_strips_workbook, date=DATE, instrument=InstrumentType.STRIPS
    )
    assert [r.isin for r in records] == ["IN000826C031"]
    assert records[0].ytm == pytest.approx(5.16)


def test_parse_curve_par_yield(tmp_path: Path, fbil_gsec_workbook_full: bytes) -> None:
    points = _source(tmp_path).parse_curve(fbil_gsec_workbook_full, product="gsec", date=DATE)
    assert [(p.tenor_years, p.ytm_semi_annual, p.ytm_annualized) for p in points] == [
        (0.25, 5.31, 5.39),
        (0.5, 5.58, 5.65),
    ]
    assert all(p.curve == "gsec_par" and p.quote_date == DATE for p in points)


def test_parse_curve_zcyc(tmp_path: Path, fbil_strips_workbook: bytes) -> None:
    points = _source(tmp_path).parse_curve(fbil_strips_workbook, product="strips", date=DATE)
    assert [p.tenor_years for p in points] == [0.25, 0.5]
    assert points[0].curve == "gsec_zcyc"


def test_fetch_curves_rejects_unknown_product(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _source(tmp_path).fetch_curves("sdl", DATE)


def test_parse_curve_missing_sheet_is_data_unavailable(
    tmp_path: Path, fbil_gsec_workbook: bytes
) -> None:
    # Pre-Feb-2023 workbooks predate the curve sheets: skip, don't fail the backfill.
    with pytest.raises(DataUnavailable, match="no 'Par Yield' sheet"):
        _source(tmp_path).parse_curve(fbil_gsec_workbook, product="gsec", date=DATE)


def test_read_or_download_serves_lake_copy_without_network(
    tmp_path: Path, fbil_gsec_workbook: bytes
) -> None:
    # No respx mock is active: any HTTP call would raise. A landed raw file must satisfy
    # the read without touching the network.
    landed = tmp_path / "raw" / "fbil" / "gsec" / f"{DATE.isoformat()}.xlsx"
    landed.parent.mkdir(parents=True)
    landed.write_bytes(fbil_gsec_workbook)
    assert _source(tmp_path).read_or_download("gsec", DATE) == fbil_gsec_workbook
