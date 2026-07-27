"""NSE CBM daily archive source — per-ISIN corporate-bond trade summaries back to 2008.

    GET https://nsearchives.nseindia.com/archives/debt/cbm/cbm_trdYYYYMMDD.csv
    (plain GET, no cookies; 404 on holidays)

The EOD archive of the exchange's CBM segment: one row per traded ISIN per session with
last/weighted-average price and yield. Same shape as the live ``liveCorp-bonds`` summaries
but reaching back ~18 years, so it extends the ``trades`` history the live feed can only
capture forward.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import Final

import httpx

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import TradeRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import DataUnavailable, SourceError

logger = get_logger(__name__)

_URL: Final = "https://nsearchives.nseindia.com/archives/debt/cbm/cbm_trd{stamp}.csv"
_SEGMENT: Final = "cbm"


class NseCbmDailySource(MetricsCollector):
    """Fetches and parses the NSE CBM daily trade-summary archive."""

    name: Final = "nse_cbm"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    # ------------------------------------------------------------------ raw fetch
    def _raw_path(self, date: dt.date) -> Path:
        return self._settings.data_dir / "raw" / "nse" / "cbm_trd" / f"{date.isoformat()}.csv"

    def download(self, date: dt.date) -> bytes:
        """Download one day's archive CSV, landing a copy in the data lake."""
        url = _URL.format(stamp=date.strftime("%Y%m%d"))
        try:
            response = self._client.get(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.NOT_FOUND:
                raise DataUnavailable(f"NSE cbm_trd not published for {date}") from exc
            raise SourceError(f"NSE cbm_trd download failed for {date}: {exc}") from exc
        content = response.content
        if not content.strip() or content.lstrip().startswith(b"<"):
            raise DataUnavailable(f"NSE cbm_trd returned a non-CSV body for {date}")
        path = self._raw_path(date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("nse_cbm.downloaded", date=date.isoformat(), bytes=len(content))
        return content

    def read_or_download(self, date: dt.date) -> bytes:
        """Return the raw CSV, serving from the data lake when already landed."""
        path = self._raw_path(date)
        if path.exists():
            return path.read_bytes()
        return self.download(date)

    # ------------------------------------------------------------------ parsing
    def fetch_trades(self, as_of: dt.date) -> list[TradeRecord]:
        """Fetch + parse one session's per-ISIN trade summaries."""
        self.reset_metrics()
        content = self.read_or_download(as_of)
        records, seen = self.parse(content)
        self.add_metric(
            f"cbm_trd/{as_of.isoformat()}",
            bytes_downloaded=len(content),
            rows_extracted=seen,
            rows_parsed=len(records),
            rows_dropped=seen - len(records),
        )
        if not records:
            raise DataUnavailable(f"NSE cbm_trd has no trades for {as_of}")
        return records

    def parse(self, content: bytes) -> tuple[list[TradeRecord], int]:
        """Parse an archive CSV into records; returns ``(records, rows_seen)``."""
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text, newline=""))
        try:
            headers = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return [], 0

        def col(prefix: str) -> int | None:
            for i, h in enumerate(headers):
                if h.startswith(prefix):
                    return i
            return None

        pos = {
            "trade_date": col("trade date"),
            "isin": col("isin"),
            "ltp": col("last trade price"),
            "total_value": col("total trade value"),
            "lty": col("last trade yield"),
            "wap": col("weighted average price"),
            "way": col("weighted average yield"),
        }
        records: list[TradeRecord] = []
        seen = 0
        for row in reader:
            seen += 1
            record = _to_record(row, pos, self.name)
            if record is not None:
                records.append(record)
        logger.info("nse_cbm.parsed", records=len(records), dropped=seen - len(records))
        return records, seen


def _to_record(row: list[str], pos: dict[str, int | None], source: str) -> TradeRecord | None:
    def cell(key: str) -> str | None:
        i = pos.get(key)
        if i is None or i >= len(row):
            return None
        value = row[i].strip()
        return value or None

    isin = cell("isin")
    trade_date = _parse_date(cell("trade_date"))
    if isin is None or len(isin) != 12 or not isin.startswith("IN") or trade_date is None:
        return None
    return TradeRecord(
        isin=isin,
        trade_date=trade_date,
        source=source,
        segment=_SEGMENT,
        ltp=_parse_float(cell("ltp")),
        lty=_parse_float(cell("lty")),
        trade_value=_parse_float(cell("total_value")),
        wap=_parse_float(cell("wap")),
        way=_parse_float(cell("way")),
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
        return dt.datetime.strptime(value, "%d-%b-%Y").date()
    except ValueError:
        return None
