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
