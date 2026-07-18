"""CDSL connector — corporate issued/outstanding half-yearly snapshots (pillar 1 + 2).

Endpoint (no auth):
``https://www.cdslindia.com/CorporateBond/IssuerReportDetails.aspx?ReportDate=DDMMYYYY`` — one big
HTML table, snapshots on 31-Mar / 30-Sep back to 2017.
See ``docs/research/2026-07-18_113141_cdslindia.com.md``.

Columns: Sr No, Name of Issuer, ISIN, Issuance Date, Maturity Date, Coupon Rate, Payment Frequency,
Embedded option, Amount issued (₹ cr), Amount outstanding (₹ cr).

Amount outstanding changes each snapshot, so it (and amount issued) are surfaced as trackable
attributes for SCD-2 history. Implements the ``UniverseFetcher`` protocol (``iter_records``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Final, cast

from lxml.html import HtmlElement, fromstring

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import InstrumentType, SecurityRecord
from bonds.sources.base import SourceError

logger = get_logger(__name__)

_URL: Final = "https://www.cdslindia.com/CorporateBond/IssuerReportDetails.aspx"

# Column order in the report table (0-based).
_COL_ISSUER: Final = 1
_COL_ISIN: Final = 2
_COL_ISSUANCE: Final = 3
_COL_MATURITY: Final = 4
_COL_COUPON: Final = 5
_COL_FREQUENCY: Final = 6
_COL_EMBEDDED: Final = 7
_COL_AMT_ISSUED: Final = 8
_COL_AMT_OUTSTANDING: Final = 9
_MIN_COLS: Final = 10


class CdslSource:
    """Fetches and parses CDSL corporate-bond issuer/outstanding snapshots."""

    name: Final = "cdsl"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    def _raw_path(self, report_date: dt.date) -> Path:
        return self._settings.data_dir / "raw" / self.name / f"{report_date.isoformat()}.html"

    def fetch_snapshot(self, report_date: dt.date) -> bytes:
        """Download the issuer-report HTML for ``report_date``, landing it in the data lake."""
        response = self._client.get(
            _URL,
            params={"ReportDate": report_date.strftime("%d%m%Y")},
            headers={"Accept": "text/html"},
        )
        content = response.content
        path = self._raw_path(report_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("cdsl.downloaded", report_date=report_date.isoformat(), bytes=len(content))
        return content

    def iter_records(
        self, as_of: dt.date, *, size: int = 100, max_pages: int | None = None
    ) -> Iterator[SecurityRecord]:
        """Yield the securities in the snapshot for ``as_of`` (a CDSL report date).

        ``size``/``max_pages`` are accepted for :class:`UniverseFetcher` compatibility and ignored
        (the report is a single whole-file table).
        """
        content = self.fetch_snapshot(as_of)
        yield from parse_snapshot(content)


def parse_snapshot(content: bytes) -> Iterator[SecurityRecord]:
    """Parse the issuer-report HTML into records (header row located by content)."""
    root = fromstring(content)
    rows = cast("list[HtmlElement]", root.xpath("//tr[td]"))
    header_seen = False
    kept = 0
    total = 0
    for row in rows:
        cells = [_text(c) for c in cast("list[HtmlElement]", row.xpath("./td"))]
        if len(cells) < _MIN_COLS:
            continue
        total += 1
        isin = cells[_COL_ISIN]
        if len(isin) != 12 or not isin.startswith("IN"):
            continue
        header_seen = True
        record = _to_record(cells)
        if record is not None:
            kept += 1
            yield record
    if not header_seen:
        raise SourceError("no ISIN rows found in CDSL report (layout changed?)")
    logger.info("cdsl.parsed", rows=total, kept=kept, dropped=total - kept)


def _to_record(cells: list[str]) -> SecurityRecord | None:
    return SecurityRecord(
        isin=cells[_COL_ISIN],
        instrument_type=InstrumentType.CORP,
        source=CdslSource.name,
        issuer=cells[_COL_ISSUER] or None,
        coupon=_as_float(cells[_COL_COUPON]),
        maturity_date=_as_date(cells[_COL_MATURITY]),
        attributes={
            "amount_outstanding_cr": cells[_COL_AMT_OUTSTANDING] or None,
            "amount_issued_cr": cells[_COL_AMT_ISSUED] or None,
            "payment_frequency": cells[_COL_FREQUENCY] or None,
            "issuance_date": cells[_COL_ISSUANCE] or None,
            "embedded_option": (cells[_COL_EMBEDDED] or None),
        },
    )


def _text(cell: HtmlElement) -> str:
    return " ".join(cell.text_content().split())


def _as_float(value: str) -> float | None:
    if not value or value.upper() in {"NA", "N/A", "-"}:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _as_date(value: str) -> dt.date | None:
    # CDSL mixes formats within the same report: "19-Mar-27" and "21-08-2026".
    value = value.strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.UTC).date()
        except ValueError:
            continue
    return None
