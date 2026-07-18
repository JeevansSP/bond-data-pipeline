"""CCIL connector — G-Sec / NDS-OM individual trades (sovereign secondary market).

CCIL is a Liferay portal behind Akamai. Flow (all verified except the open-market response shape):
  1. GET the individual-trades page to prime cookies.
  2. POST the ``ticker`` portlet resource -> ``{"resultMarketOpenClose": "Y"|"N"}`` (market gate).
  3. If open, POST the ``main`` resource per Sec Type (CG/SG/TB) with the form params to get trades.

Portlet POST (needs cookies + ``X-Requested-With`` + a form body so Content-Length is set)::

    POST /individual-trades?p_p_id=<PORTLET>&p_p_lifecycle=2&p_p_resource_id=main&...
    body: <ns>_market=C  <ns>_subType=RGLR  <ns>_secType=CG|SG|TB  <ns>_security=

Codes: market {C=Regular, W=When Issued}; subType {RGLR=Standard, ODDX=Odd Lot};
secType {CG=Central Govt, SG=State Govt, TB=Tbills}.
See ``docs/research/2026-07-18_113141_ccilindia.com.md``.

NOTE: the ``main`` data resource returns "Undeployed" outside market hours, so the exact
trade-row JSON shape could not be observed (weekend). The parser is best-effort over a JSON
record list with flexible field detection; every raw response is landed for weekday validation.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Final

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import TradeRecord

logger = get_logger(__name__)

_PAGE: Final = "https://www.ccilindia.com/individual-trades"
_PORTLET: Final = "com_ccil_individual_trades_CcilIndividualTradesMVCPortlet_INSTANCE_bxkl"
_NS: Final = f"_{_PORTLET}_"
_RESOURCE_URL: Final = (
    f"{_PAGE}?p_p_id={_PORTLET}&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
    "&p_p_cacheability=cacheLevelPage&p_p_resource_id="
)
_PAGE_HEADERS: Final = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_POST_HEADERS: Final = {
    "Accept": "application/json, text/javascript, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": _PAGE,
}
# Sec Type code -> segment label stored on the trade.
_SEC_TYPES: Final[tuple[str, ...]] = ("CG", "SG", "TB")
_ISIN_KEYS: Final = ("isin", "ISIN", "securityIsin", "security", "Security")
_PRICE_KEYS: Final = ("price", "Price", "tradePrice", "ltp", "rate", "Rate")
_YIELD_KEYS: Final = ("yield", "Yield", "ytm", "YTM", "tradeYield")


class CcilSource:
    """Fetches NDS-OM individual trades (market-hours only; empty when the market is closed)."""

    name: Final = "ccil"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    def fetch_trades(self, as_of: dt.date) -> list[TradeRecord]:
        """Prime cookies, gate on market status, then fetch + parse trades per Sec Type."""
        self._client.get(_PAGE, headers=_PAGE_HEADERS)  # cookie priming
        if not self._market_open():
            logger.info("ccil.market_closed", as_of=as_of.isoformat())
            return []

        records: list[TradeRecord] = []
        for sec_type in _SEC_TYPES:
            response = self._client.post(
                f"{_RESOURCE_URL}main",
                data={
                    f"{_NS}market": "C",
                    f"{_NS}subType": "RGLR",
                    f"{_NS}secType": sec_type,
                    f"{_NS}security": "",
                },
                headers=_POST_HEADERS,
            )
            self._land(as_of, sec_type, response.text)
            records.extend(parse_main(response.text, sec_type, as_of))
        return records

    def _market_open(self) -> bool:
        response = self._client.post(f"{_RESOURCE_URL}ticker", data={}, headers=_POST_HEADERS)
        try:
            return str(response.json().get("resultMarketOpenClose", "N")).upper() == "Y"
        except (ValueError, AttributeError):
            return False

    def _land(self, as_of: dt.date, sec_type: str, text: str) -> None:
        path = self._settings.data_dir / "raw" / self.name / as_of.isoformat() / f"{sec_type}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def parse_main(text: str, sec_type: str, as_of: dt.date) -> list[TradeRecord]:
    """Best-effort parse of a ``main`` response into trades (shape pending weekday validation)."""
    if not text or text.strip() == "Undeployed":
        return []
    try:
        payload: Any = json.loads(text)
    except ValueError:
        logger.warning("ccil.unparsed_response", sec_type=sec_type)
        return []
    rows = _extract_rows(payload)
    records: list[TradeRecord] = []
    for row in rows:
        record = _row_to_trade(row, sec_type, as_of)
        if record is not None:
            records.append(record)
    return records


def _extract_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "aaData", "rows", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _row_to_trade(row: Any, sec_type: str, as_of: dt.date) -> TradeRecord | None:
    if not isinstance(row, dict):
        return None
    isin = _first(row, _ISIN_KEYS)
    if not isinstance(isin, str) or len(isin.strip()) != 12 or not isin.strip().startswith("IN"):
        return None
    return TradeRecord(
        isin=isin.strip(),
        trade_date=as_of,
        source=CcilSource.name,
        segment=sec_type,
        ltp=_as_float(_first(row, _PRICE_KEYS)),
        lty=_as_float(_first(row, _YIELD_KEYS)),
    )


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
