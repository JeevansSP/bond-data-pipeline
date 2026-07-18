"""SEBI connector — corporate-bond public-issue calendar (primary market).

Endpoint (static HTML; needs browser-like Accept headers or it 530s):
``https://www.sebi.gov.in/statistics/corporate-bonds/publicissuedata.html`` — one table per
financial year, FY2008-09 onward. Columns: S.No, Name of company, Issue opened/closed on,
Base issue size (₹ cr), Final Issue size (₹ cr). Public issues only (no private placements/ISINs).
See ``docs/research/2026-07-18_113141_sebi.gov.in.md``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Final, cast

from lxml.html import HtmlElement, fromstring

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import PublicIssueRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import SourceError

logger = get_logger(__name__)

_URL: Final = "https://www.sebi.gov.in/statistics/corporate-bonds/publicissuedata.html"
_HEADERS: Final = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_FY_START_MONTH: Final = 4
_MIN_COLS: Final = 6
_COL_COMPANY: Final = 1
_COL_OPEN: Final = 2
_COL_CLOSE: Final = 3
_COL_BASE: Final = 4
_COL_FINAL: Final = 5


class SebiSource(MetricsCollector):
    """Fetches and parses the SEBI public-issue calendar."""

    name: Final = "sebi"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    def _raw_path(self, as_of: dt.date) -> Path:
        return (
            self._settings.data_dir / "raw" / self.name / f"public_issues_{as_of.isoformat()}.html"
        )

    def fetch_public_issues(self, as_of: dt.date) -> list[PublicIssueRecord]:
        """Download the page (landing it) and parse all financial-year tables."""
        self.reset_metrics()
        response = self._client.get(_URL, headers=_HEADERS)
        content = response.content
        path = self._raw_path(as_of)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("sebi.downloaded", bytes=len(content))
        records = parse_public_issues(content)
        self.add_metric(
            "public_issues",
            bytes_downloaded=len(content),
            rows_extracted=len(records),
            rows_parsed=len(records),
        )
        return records


def parse_public_issues(content: bytes) -> list[PublicIssueRecord]:
    """Parse every FY table into public-issue records (skips header/total rows)."""
    root = fromstring(content)
    tables = cast("list[HtmlElement]", root.xpath("//table"))
    records: list[PublicIssueRecord] = []
    for table in tables:
        headers = " ".join(table.text_content().split()).lower()
        if "name of company" not in headers:
            continue
        for row in cast("list[HtmlElement]", table.xpath(".//tr[td]")):
            cells = [_text(c) for c in cast("list[HtmlElement]", row.xpath("./td"))]
            record = _to_record(cells)
            if record is not None:
                records.append(record)
    if not records:
        raise SourceError("no public-issue rows parsed from SEBI page (layout changed?)")
    logger.info("sebi.parsed", records=len(records))
    return records


def _to_record(cells: list[str]) -> PublicIssueRecord | None:
    if len(cells) < _MIN_COLS:
        return None  # "Total" summary rows and the like
    company = cells[_COL_COMPANY]
    issue_open = _as_date(cells[_COL_OPEN])
    if not company or company.lower() == "total" or issue_open is None:
        return None
    return PublicIssueRecord(
        company=company,
        issue_open=issue_open,
        source=SebiSource.name,
        issue_close=_as_date(cells[_COL_CLOSE]),
        base_size_cr=_as_float(cells[_COL_BASE]),
        final_size_cr=_as_float(cells[_COL_FINAL]),
        financial_year=_financial_year(issue_open),
    )


def _financial_year(day: dt.date) -> str:
    """Indian financial year label for a date, e.g. 2009-02-02 -> ``2008-09``."""
    start = day.year if day.month >= _FY_START_MONTH else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _text(cell: HtmlElement) -> str:
    return " ".join(cell.text_content().split())


def _as_float(value: str) -> float | None:
    value = value.strip().replace(",", "")
    if not value or value.upper() in {"NA", "N/A", "-"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_date(value: str) -> dt.date | None:
    value = value.strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.UTC).date()
        except ValueError:
            continue
    return None
