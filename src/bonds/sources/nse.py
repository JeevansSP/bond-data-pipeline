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
import re
from typing import Any, Final

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import InstrumentType, SecurityRecord, TradeRecord
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
            trade_date = _parse_timestamp(payload.get("timestamp"))
            if trade_date is None:
                # Surface envelope-timestamp drift: silently stamping the run date would
                # mislabel last-session data whenever the run day isn't the trading day.
                logger.warning(
                    "nse.timestamp_unparsed",
                    segment=segment,
                    raw=str(payload.get("timestamp"))[:40],
                )
                trade_date = as_of
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
        return int(float(str(value).replace(",", "")))  # handle Indian-grouped / float-ish strings
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------- securities-master derivation
# Some NSE-traded corporate ISINs never appear in the BondCentral/CDSL universe pulls (fresh
# private placements, unlisted-segment paper). Derive a minimal reference row from the trade
# descriptor so trades never orphan; the enrichment pass fills coupon/maturity properly later
# (a missing coupon is exactly its selection criterion).
#
# Descriptors look like "KRAZYBEE SERVICES LIMITED 10.65 NCD 12AG27 FVRS10LAC" — issuer text,
# then optionally a coupon and series/date noise. The issuer is cut at a series/instrument
# marker, a decimal number (the coupon) or a 4+-digit run (a year) — NOT at bare short digits,
# which occur inside real legal names ("ONE 97 COMMUNICATIONS LIMITED", "M 3 M INDIA").
_ISSUER_END_RE: Final = re.compile(
    r"\s+(?:SR\b|SERIES\b|TR\b|NCD\b|MLD\b|BD\b|FVRS|\d+\.\d|\d{4,})"
)
# Coupon: a standalone decimal (not part of a dotted date like "26.09.2025"), sanity-bounded —
# a wrongly-derived coupon is sticky (it permanently excludes the row from enrichment).
_COUPON_RE: Final = re.compile(r"(?<![\d.])(\d{1,2}\.\d{1,4})(?![.\d])")
_COUPON_MAX: Final = 30.0
_MIN_ISSUER_LEN: Final = 3  # a one-letter "issuer" (real descriptor: just "L") is noise


def _derive_issuer(descriptor: str) -> str | None:
    match = _ISSUER_END_RE.search(descriptor)
    issuer = (descriptor[: match.start()] if match else descriptor).strip(" -.,")
    return issuer if len(issuer) >= _MIN_ISSUER_LEN else None


def _derive_coupon(descriptor: str) -> float | None:
    match = _COUPON_RE.search(descriptor)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 < value <= _COUPON_MAX else None


def derive_security(isin: str, descriptor: str | None) -> SecurityRecord:
    """Best-effort minimal reference row for one NSE-traded corporate ISIN."""
    desc = (descriptor or "").strip() or None
    return SecurityRecord(
        isin=isin,
        instrument_type=InstrumentType.CORP,
        source=NseSource.name,
        description=desc,
        issuer=_derive_issuer(desc) if desc else None,
        coupon=_derive_coupon(desc) if desc else None,
    )


def derive_securities(trades: list[TradeRecord]) -> list[SecurityRecord]:
    """Reference securities for a batch of NSE trades (one per ISIN, last descriptor wins)."""
    by_isin: dict[str, SecurityRecord] = {}
    for t in trades:
        by_isin[t.isin] = derive_security(t.isin, t.descriptor)
    return list(by_isin.values())
