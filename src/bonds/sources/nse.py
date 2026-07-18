"""NSE connector — exchange corporate-bond trade feed (secondary market, forward capture).

Akamai-gated: prime cookies by GETting the page, then call the JSON API on the *same* client
(httpx persists the cookie jar). API:
``GET https://www.nseindia.com/api/liveCorp-bonds?index=<segment>&marketType=CBM``
segments: otctrades_listed / otctrades_unlisted / exchtrades_listed / exchtrades_unlisted.
Row: {descriptor, isin, ltp, lty, noOfTrades, tradeValue, wap, way}; envelope carries a
last-session ``timestamp`` (used as the trade date).
See ``docs/research/2026-07-18_113141_nseindia.com.md``.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Final

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import TradeRecord
from bonds.quality.metrics import MetricsCollector

logger = get_logger(__name__)

_PAGE: Final = (
    "https://www.nseindia.com/market-data/debt-market-reporting-corporate-bonds-traded-on-exchange"
)
_API: Final = "https://www.nseindia.com/api/liveCorp-bonds"
_MARKET_TYPE: Final = "CBM"
_SEGMENTS: Final = (
    "otctrades_listed",
    "otctrades_unlisted",
    "exchtrades_listed",
    "exchtrades_unlisted",
)
_PAGE_HEADERS: Final = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_API_HEADERS: Final = {"Accept": "*/*", "Referer": _PAGE}


class NseSource(MetricsCollector):
    """Fetches NSE corporate-bond trade summaries across all four CBM segments."""

    name: Final = "nse"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    def fetch_trades(self, as_of: dt.date) -> list[TradeRecord]:
        """Prime Akamai cookies, then fetch + parse every segment's trades."""
        self.reset_metrics()
        self._client.get(_PAGE, headers=_PAGE_HEADERS)  # cookie priming
        records: list[TradeRecord] = []
        for segment in _SEGMENTS:
            response = self._client.get(
                _API, params={"index": segment, "marketType": _MARKET_TYPE}, headers=_API_HEADERS
            )
            payload: dict[str, Any] = response.json()
            self._land(as_of, segment, payload)
            trade_date = _parse_timestamp(payload.get("timestamp")) or as_of
            rows = payload.get("data") or []
            kept = 0
            for row in rows:
                record = _to_record(row, segment, trade_date)
                if record is not None:
                    kept += 1
                    records.append(record)
            self.add_metric(
                segment,
                bytes_downloaded=len(response.content),
                rows_extracted=len(rows),
                rows_parsed=kept,
                rows_dropped=len(rows) - kept,
            )
            logger.info(
                "nse.segment", segment=segment, rows=len(rows), trade_date=trade_date.isoformat()
            )
        return records

    def _land(self, as_of: dt.date, segment: str, payload: dict[str, Any]) -> None:
        path = self._settings.data_dir / "raw" / self.name / as_of.isoformat() / f"{segment}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


def _to_record(row: dict[str, Any], segment: str, trade_date: dt.date) -> TradeRecord | None:
    isin = str(row.get("isin") or "").strip()
    if len(isin) != 12 or not isin.startswith("IN"):
        return None
    return TradeRecord(
        isin=isin,
        trade_date=trade_date,
        source=NseSource.name,
        segment=segment,
        descriptor=_as_str(row.get("descriptor")),
        ltp=_as_float(row.get("ltp")),
        lty=_as_float(row.get("lty")),
        no_of_trades=_as_int(row.get("noOfTrades")),
        trade_value=_as_float(row.get("tradeValue")),
        wap=_as_float(row.get("wap")),
        way=_as_float(row.get("way")),
    )


def _parse_timestamp(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.strptime(value.strip(), "%d-%b-%Y %H:%M").replace(tzinfo=dt.UTC).date()
    except ValueError:
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))  # NSE returns Indian-grouped numbers
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
