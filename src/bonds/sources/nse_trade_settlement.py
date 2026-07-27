"""NSE corporate-bond Trade & Settlement source (trade-level, RFQ + OTC-reported).

    GET https://www.nseindia.com/api/historicalOR/tradeSettlementDebt
        ?from=DD-MM-YYYY&to=DD-MM-YYYY&csv=true

Akamai-gated like the other NSE endpoints: prime cookies by GETting the report page on the
same client. The JSON form caps at 70 rows; ``csv=true`` returns the complete file. Windows
are capped at 7 days per request. History reaches back to ~Jan-2010 (thin before 2020).
Null cells are ``-``; dates are ``DD-MM-YYYY``; trade time is minute-resolution.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import Final

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import CorporateTradeRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import DataUnavailable

logger = get_logger(__name__)

_PAGE: Final = "https://www.nseindia.com/report-detail/tradeSettlement-debt"
_API: Final = "https://www.nseindia.com/api/historicalOR/tradeSettlementDebt"
_PAGE_HEADERS: Final = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_API_HEADERS: Final = {"Accept": "*/*", "Referer": _PAGE}

# CSV header (stripped, lowercased) -> record field. Headers carry trailing spaces upstream.
_COLUMNS: Final = {
    "seller deal type": "seller_deal_type",
    "buyer deal type": "buyer_deal_type",
    "isin": "isin",
    "listed/unlisted security": "listed",
    "issuer name": "issuer",
    "issue description": "description",
    "yield type": "yield_type",
    "outside yield range": "outside_yield_range",
    "reported trade/trade executed on rfq platform": "venue",
    "remarks": "remarks",
    "settlement status": "settlement_status",
}


class NseTradeSettlementSource(MetricsCollector):
    """Fetches and parses NSE debt Trade & Settlement (trade-level) data."""

    name: Final = "nse"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)
        self._primed = False

    # ------------------------------------------------------------------ raw fetch
    def _raw_path(self, start: dt.date, end: dt.date) -> Path:
        return (
            self._settings.data_dir
            / "raw"
            / self.name
            / "trade_settlement"
            / f"{start.isoformat()}_{end.isoformat()}.csv"
        )

    def download(self, start: dt.date, end: dt.date) -> bytes:
        """Download one window (max 7 days) of trades, landing a copy in the data lake."""
        if (end - start).days > 6:
            raise ValueError(f"NSE caps trade-settlement windows at 7 days: {start}..{end}")
        if not self._primed:
            self._client.get(_PAGE, headers=_PAGE_HEADERS)  # cookie priming
            self._primed = True
        response = self._client.get(
            _API,
            params={
                "from": start.strftime("%d-%m-%Y"),
                "to": end.strftime("%d-%m-%Y"),
                "csv": "true",
            },
            headers=_API_HEADERS,
        )
        content = response.content
        if content.lstrip().startswith(b"<"):
            raise DataUnavailable(f"NSE returned a non-CSV body for {start}..{end}")
        path = self._raw_path(start, end)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info(
            "nse_ts.downloaded",
            start=start.isoformat(),
            end=end.isoformat(),
            bytes=len(content),
        )
        return content

    def read_or_download(self, start: dt.date, end: dt.date) -> bytes:
        """Return a window's raw CSV, serving from the data lake when already landed."""
        path = self._raw_path(start, end)
        if path.exists():
            return path.read_bytes()
        return self.download(start, end)

    # ------------------------------------------------------------------ parsing
    def fetch_trades(self, start: dt.date, end: dt.date) -> list[CorporateTradeRecord]:
        """Fetch + parse one window's trade-level records.

        Raises:
            DataUnavailable: When the window has no trades (holiday-only window).
        """
        self.reset_metrics()
        content = self.read_or_download(start, end)
        records, seen = self.parse(content)
        self.add_metric(
            f"trade_settlement/{start.isoformat()}_{end.isoformat()}",
            bytes_downloaded=len(content),
            rows_extracted=seen,
            rows_parsed=len(records),
            rows_dropped=seen - len(records),
        )
        if not records:
            raise DataUnavailable(f"NSE has no corporate trades for {start}..{end}")
        return records

    def parse(self, content: bytes) -> tuple[list[CorporateTradeRecord], int]:
        """Parse a raw window CSV into records; returns ``(records, rows_seen)``."""
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text, newline=""))
        try:
            headers = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return [], 0
        index = {
            name: headers.index(header) for header, name in _COLUMNS.items() if header in headers
        }
        pos = {
            "coupon": _find(headers, "coupon"),
            "price": _find(headers, "price"),
            "trade_yield": _find(headers, "yield"),
            "put_call_date": _find(headers, "put/call date"),
            "trade_value_lakh": _find(headers, "trade value"),
            "trade_time": _find(headers, "trade date & time"),
            "settlement_date": _find(headers, "settlement date"),
        }
        records: list[CorporateTradeRecord] = []
        seen = 0
        for row in reader:
            seen += 1
            record = _to_record(row, index, pos, self.name)
            if record is not None:
                records.append(record)
        logger.info("nse_ts.parsed", records=len(records), dropped=seen - len(records))
        return records, seen


def _find(headers: list[str], name: str) -> int | None:
    return headers.index(name) if name in headers else None


def _to_record(
    row: list[str],
    index: dict[str, int],
    pos: dict[str, int | None],
    source: str,
) -> CorporateTradeRecord | None:
    def cell(i: int | None) -> str | None:
        if i is None or i >= len(row):
            return None
        value = row[i].strip()
        return value if value and value != "-" else None

    isin = cell(index.get("isin"))
    if isin is None or len(isin) != 12 or not isin.startswith("IN"):
        return None
    trade_time = _parse_datetime(cell(pos["trade_time"]))
    if trade_time is None:
        return None  # a trade without a trade date cannot be keyed to a session
    return CorporateTradeRecord(
        isin=isin,
        trade_date=trade_time.date(),
        source=source,
        trade_time=trade_time,
        listed=cell(index.get("listed")),
        seller_deal_type=cell(index.get("seller_deal_type")),
        buyer_deal_type=cell(index.get("buyer_deal_type")),
        issuer=cell(index.get("issuer")),
        description=cell(index.get("description")),
        coupon=_parse_float(cell(pos["coupon"])),
        price=_parse_float(cell(pos["price"])),
        trade_yield=_parse_float(cell(pos["trade_yield"])),
        yield_type=cell(index.get("yield_type")),
        outside_yield_range=cell(index.get("outside_yield_range")),
        put_call_date=_parse_date(cell(pos["put_call_date"])),
        trade_value_lakh=_parse_float(cell(pos["trade_value_lakh"])),
        settlement_date=_parse_date(cell(pos["settlement_date"])),
        settlement_status=cell(index.get("settlement_status")),
        venue=cell(index.get("venue")),
        remarks=cell(index.get("remarks")),
    )


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
        return dt.datetime.strptime(value, "%d-%m-%Y").date()
    except ValueError:
        return None


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
