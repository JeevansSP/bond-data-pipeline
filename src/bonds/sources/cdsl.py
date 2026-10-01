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
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from lxml.html import HtmlElement, fromstring

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import InstrumentType, SecurityRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import DataUnavailable, SourceError

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
# An unpublished report date renders one full-width placeholder row: ten cells, nine empty.
_NO_RECORDS: Final = "no records found"


class CdslSource(MetricsCollector):
    """Fetches and parses CDSL corporate-bond issuer/outstanding snapshots."""

    name: Final = "cdsl"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
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
        self.reset_metrics()
        content = self.fetch_snapshot(as_of)
        parsed = parse_snapshot(content)
        self.add_metric(
            as_of.isoformat(),
            bytes_downloaded=len(content),
            rows_extracted=parsed.rows_seen,
            rows_parsed=len(parsed.records),
            rows_dropped=parsed.rows_seen - len(parsed.records),
        )
        yield from parsed.records


@dataclass(frozen=True, slots=True)
class ParsedSnapshot:
    """Records plus funnel counts from one issuer-report HTML."""

    records: list[SecurityRecord]
    rows_seen: int
    """Candidate data rows (enough cells), before ISIN validity filtering."""

    def __iter__(self) -> Iterator[SecurityRecord]:
        """Iterate the parsed records (keeps ``for r in parse_snapshot(...)`` working)."""
        return iter(self.records)


def parse_snapshot(content: bytes) -> ParsedSnapshot:
    """Parse the issuer-report HTML into records (data rows located by ISIN content)."""
    root = fromstring(content)
    rows = cast("list[HtmlElement]", root.xpath("//tr[td]"))
    records: list[SecurityRecord] = []
    candidates = 0
    for row in rows:
        cells = [_text(c) for c in cast("list[HtmlElement]", row.xpath("./td"))]
        if len(cells) < _MIN_COLS:
            continue
        if " ".join(c for c in cells if c).lower() == _NO_RECORDS:
            # The placeholder is as wide as a data row, so the width test above lets it through;
            # counting it would turn "not published yet" into "layout changed?" — which failed
            # every nightly run on the 30-Sep-2026 snapshot until CDSL posted it.
            continue
        candidates += 1
        isin = cells[_COL_ISIN]
        if len(isin) != 12 or not isin.startswith("IN"):
            continue
        records.append(_to_record(cells))
    if candidates == 0:
        # CDSL serves the page shell for a report date it has not published yet (the 31-Mar /
        # 30-Sep snapshots appear days late). That is "not available", so it must record SKIPPED
        # and be re-attempted, not FAILED.
        raise DataUnavailable("CDSL has not published this report date yet (no data rows)")
    if not records:
        raise SourceError("no ISIN rows found in CDSL report (layout changed?)")
    logger.info(
        "cdsl.parsed", rows=candidates, kept=len(records), dropped=candidates - len(records)
    )
    return ParsedSnapshot(records=records, rows_seen=candidates)


def _to_record(cells: list[str]) -> SecurityRecord:
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
    # Coupons come plain ("7.79") and %-suffixed ("10.03%"), sometimes with a footnote
    # star ("9.24%*") — ~78% of live snapshot rows carry the % suffix. rstrip with a char
    # set handles either suffix order.
    cleaned = value.replace(",", "").rstrip("*%").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _as_date(value: str) -> dt.date | None:
    # CDSL mixes formats within the same report: "19-Mar-27" and "21-08-2026".
    value = value.strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value, fmt).replace(tzinfo=dt.UTC).date()
        except ValueError:
            continue
        if fmt == "%d-%b-%y" and parsed.year < 2000:
            # strptime pivots two-digit years 69-99 into 19xx, but nothing in these snapshots
            # (2017+) matures in the past century: "31-Dec-99" is CDSL's 2099-12-31 perpetual
            # placeholder (nulled downstream by the model's plausible-maturity window) and any
            # genuine post-2068 maturity would otherwise be poisoned into the 1900s.
            parsed = parsed.replace(year=parsed.year + 100)
        return parsed
    return None
