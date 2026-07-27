"""Shared test fixtures."""

from __future__ import annotations

import datetime as dt
import io

import openpyxl
import pytest


@pytest.fixture
def fbil_gsec_workbook() -> bytes:
    """A synthetic FBIL G-Sec workbook mirroring the real layout.

    Leading branding/title rows precede the ISIN header, so tests exercise the
    content-based header detection rather than a hard-coded offset.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "G-Sec"
    ws.append([None, "Financial Benchmarks India"])
    ws.append([])
    ws.append(["FBIL GSec Prices/Yields", None, "10-Jul-2026"])
    ws.append([])
    ws.append(
        [
            "ISIN",
            "Coupon",
            "Maturity(dd-mmm-yyyy)",
            "Price(Rs)",
            "YTM% p.a. (Semi-Annual)",
            "Remark 1",
            "Remark 2",
        ]
    )
    ws.append(["IN0020160035", 6.97, dt.datetime(2026, 9, 6), 100.2374, 5.265, None, None])
    ws.append(["IN0020010081", 10.18, dt.datetime(2026, 9, 11), 100.7571, 5.3064, None, None])
    ws.append([None, None, None, None, None])  # trailing blank row -> ignored
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def fbil_gsec_workbook_full() -> bytes:
    """A synthetic FBIL G-Sec workbook with all four real sheets.

    Mirrors the production layout: per-ISIN "G-Sec", the "Par Yield" curve, per-ISIN
    "Special" (GoI special securities, with a group-label row), and the non-data
    "Note on FRB & IIB" tab.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "G-Sec"
    ws.append([None, "Financial Benchmarks India"])
    ws.append(["FBIL GSec Prices/Yields", None, "10-Jul-2026"])
    ws.append(["ISIN", "Coupon", "Maturity(dd-mmm-yyyy)", "Price(Rs)", "YTM% p.a. (Semi-Annual)"])
    ws.append(["IN0020160035", 6.97, dt.datetime(2026, 9, 6), 100.2374, 5.265])

    par = wb.create_sheet("Par Yield")
    par.append(["Financial Benchmarks India"])
    par.append(["FBIL GSec Base/Par Yield", None, "10-Jul-2026"])
    par.append(["Tenor (Year)", "YTM% p.a.(Semi-Annual)", "YTM % p.a.(Annualized)"])
    par.append([0.25, 5.31, 5.39])
    par.append([0.5, 5.58, 5.65])
    par.append([None, None, None])  # trailing blank -> end of grid

    special = wb.create_sheet("Special")
    special.append([None, "Financial Benchmarks India"])
    special.append(["FBIL SPL Security Prices/Yields", None, None, "10-Jul-2026"])
    special.append(
        ["ISIN", "Description", "Coupon", "Maturity (dd-mmm-yyyy)", "Price(Rs)", "YTM% p.a."]
    )
    special.append([" ", "OTHER SPECIAL SECURITIES", None, None, None, None])  # group label
    special.append(
        ["IN0020060029", "08.23 GOI FCI 2027", 8.23, dt.datetime(2027, 2, 12), 101.2, 5.85]
    )

    note = wb.create_sheet("Note on FRB & IIB")
    note.append([None, "Note on FRB", "10-Jul-2026"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def fbil_strips_workbook() -> bytes:
    """A synthetic FBIL STRIPS workbook: per-STRIP prices + the GOI ZCYC curve sheet."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "STRIPS"
    ws.append([None, "Financial Benchmarks India"])
    ws.append([None, "FBIL - GOI STRIP Prices", None, "10-Jul-26"])
    # Real header says "Yield%" (not "YTM…") — exercises the yield-column matcher.
    ws.append(
        ["ISIN", "Coupon STRIPS", "Maturity (dd-mmm-yyyy)", "Price(Rs)", "Yield%\n(Semi-Annual)"]
    )
    ws.append(["IN000826C031", "CS 05 AUG 2026", dt.datetime(2026, 8, 5), 99.89, 5.16])

    zcyc = wb.create_sheet("ZCYC")
    zcyc.append(["Financial Benchmarks India"])
    zcyc.append(["FBIL - GOI Zero Coupon Yields", None, "10-Jul-2026"])
    zcyc.append(["Tenor (Year)", "Zero Coupon%  (Semi-annual)", "Zero Coupon % (Annualized)"])
    zcyc.append([0.25, 5.35, 5.42])
    zcyc.append([0.5, 5.58, 5.66])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
