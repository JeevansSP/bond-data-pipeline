"""NSE Corporate Bond Report source — the listed-bond master with cash-flow fields.

    GET https://nsearchives.nseindia.com/content/debt/Corporate_bond_report_DD-Mon-YYYY.csv
    (plain GET, no cookies; 404 on holidays and before the feed's ~Dec-2019 start)

One daily snapshot of every listed corporate bond with the fields the valuation Directions
need and no other source carries: **day count convention, coupon frequency, next coupon
date**, floating benchmark + spread, issue date, face value, credit rating and status.
Feeds the universe pipeline: identity fields upsert ``securities``; the changeable fields
go to SCD-2 attribute history.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import httpx

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import InstrumentType, SecurityRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import DataUnavailable, SourceError

logger = get_logger(__name__)

_URL: Final = "https://nsearchives.nseindia.com/content/debt/Corporate_bond_report_{stamp}.csv"

# Report header -> SecurityRecord.attributes key (SCD-2 tracked, changeable over time).
_ATTRIBUTE_COLUMNS: Final = {
    "Credit Rating": "credit_rating_nse",
    "Step up Coupons": "step_up_coupons",
    "Coupon Frequency": "coupon_frequency",
    "Next Coupon Date": "next_coupon_date",
    "Day Count Convention": "day_count_convention",
    "Floating Benchmark": "floating_benchmark",
    "Spread over the Benchmark": "benchmark_spread",
    "Issue Date": "issuance_date",
    # NOT "security_status": NSE's vocabulary ("Listed") differs from BondCentral's
    # ("ACTIVE"); sharing the key would flip-flop SCD-2 history daily between sources.
    "Status": "listing_status",
}

_COUPON_RE: Final = re.compile(r"^(\d+(?:\.\d+)?)%$")


class NseBondReportSource(MetricsCollector):
    """Fetches and parses the daily NSE corporate bond report."""

    name: Final = "nse_cbr"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    # ------------------------------------------------------------------ raw fetch
    def _raw_path(self, date: dt.date) -> Path:
        return (
            self._settings.data_dir
            / "raw"
            / "nse"
            / "corporate_bond_report"
            / f"{date.isoformat()}.csv"
        )

    def download(self, date: dt.date) -> bytes:
        """Download one day's report, landing a copy in the data lake."""
        url = _URL.format(stamp=date.strftime("%d-%b-%Y"))
        try:
            response = self._client.get(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.NOT_FOUND:
                raise DataUnavailable(f"NSE bond report not published for {date}") from exc
            raise SourceError(f"NSE bond report download failed for {date}: {exc}") from exc
        content = response.content
        if not content.strip() or content.lstrip().startswith(b"<"):
            raise DataUnavailable(f"NSE bond report returned a non-CSV body for {date}")
        path = self._raw_path(date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("nse_cbr.downloaded", date=date.isoformat(), bytes=len(content))
        return content

    def read_or_download(self, date: dt.date) -> bytes:
        """Return the raw report, serving from the data lake when already landed."""
        path = self._raw_path(date)
        if path.exists():
            return path.read_bytes()
        return self.download(date)

    # ------------------------------------------------------------------ parsing
    def iter_records(
        self, as_of: dt.date, *, size: int = 500, max_pages: int | None = None
    ) -> Iterator[SecurityRecord]:
        """Yield the day's bond master (``size``/``max_pages`` accepted for protocol parity)."""
        self.reset_metrics()
        content = self.read_or_download(as_of)
        records, seen = self._parse_with_stats(content)
        self.add_metric(
            f"corporate_bond_report/{as_of.isoformat()}",
            bytes_downloaded=len(content),
            rows_extracted=seen,
            rows_parsed=len(records),
            rows_dropped=seen - len(records),
        )
        yield from records

    def _parse_with_stats(self, content: bytes) -> tuple[list[SecurityRecord], int]:
        text = content.decode("utf-8-sig", errors="replace")
        header: dict[str, int] = {}
        records: list[SecurityRecord] = []
        seen = 0
        # The file interleaves branding rows and repeated section headers with data rows;
        # track the most recent header row and parse everything under it that carries an ISIN.
        for row in csv.reader(io.StringIO(text, newline="")):
            if row and row[0].strip() == "Sectype":
                header = {cell.strip(): i for i, cell in enumerate(row)}
                continue
            if not header:
                continue
            seen += 1
            record = _to_record(row, header, self.name)
            if record is not None:
                records.append(record)
        logger.info("nse_cbr.parsed", records=len(records), dropped=seen - len(records))
        return records, seen


def _to_record(row: list[str], header: dict[str, int], source: str) -> SecurityRecord | None:
    def cell(name: str) -> str | None:
        i = header.get(name)
        if i is None or i >= len(row):
            return None
        value = row[i].strip()
        return value if value and value != "-" else None

    isin = cell("ISIN")
    if isin is None or len(isin) != 12 or not isin.startswith("IN"):
        return None
    coupon = _parse_coupon(cell("Issue Name"))
    floating = cell("Floating Benchmark")
    interest_type = "Floating" if floating else ("Fixed" if coupon else None)
    next_coupon = _parse_date(cell("Next Coupon Date"))
    issue_date = _parse_date(cell("Issue Date"))
    return SecurityRecord(
        isin=isin,
        instrument_type=InstrumentType.CORP,
        source=source,
        description=cell("Issue Desc"),
        issuer=cell("Issuer"),
        coupon=coupon,
        interest_type=interest_type,
        maturity_date=_parse_date(cell("Maturity Date")),
        face_value=_parse_float(cell("Face Value")),
        attributes={attr: cell(column) for column, attr in _ATTRIBUTE_COLUMNS.items()}
        | {
            # Re-serialize dates as ISO so history comparisons are format-stable.
            "next_coupon_date": next_coupon.isoformat() if next_coupon else None,
            "issuance_date": issue_date.isoformat() if issue_date else None,
        },
    )


def _parse_coupon(value: str | None) -> float | None:
    if value is None:
        return None
    match = _COUPON_RE.match(value)
    return float(match.group(1)) if match else None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _parse_date(value: str | None) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.datetime.strptime(value, "%d-%b-%Y").date()
    except ValueError:
        return None
