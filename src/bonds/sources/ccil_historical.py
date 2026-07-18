"""CCIL G-Sec Historical Trades — downloadable trade-by-trade history (G-Sec, SDL, T-Bill).

Unlike the live NDS-OM portlet (market-hours only), this is a **historical** download that works for
any date range. Flow (verified; pure httpx, no browser):

    1. GET https://www.ccilindia.com/g-sec-historical-trades          (prime Akamai cookies)
    2. POST .../g-sec-historical-trades?...&p_p_resource_id=serveResource
       body: <NS>fromDate1=YYYY-MM-DD, <NS>toDate1=YYYY-MM-DD,
             <NS>hidFrom=AES(fromDate1), <NS>hidTo=AES(toDate1)
       -> CSV: Trade date, Time, ISIN, Description, Face Value, Trade Price, YTM/Yield, Indicator

The date params are AES-128-ECB/PKCS7 base64-encrypted client-side, but the key is hardcoded in
CCIL's JS (``mustbe16byteskey``), so we reproduce it in Python.
See ``docs/research/2026-07-18_113141_ccilindia.com.md``.

Trade-by-trade rows are aggregated to one per ISIN per day (VWAP price/yield, count, total value),
matching the ``trades`` table shape used by NSE; the raw CSV is landed for finer granularity.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
from collections import defaultdict
from typing import Final

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import TradeRecord
from bonds.quality.metrics import MetricsCollector

logger = get_logger(__name__)

_PAGE: Final = "https://www.ccilindia.com/g-sec-historical-trades"
_PORTLET: Final = "NewTradeByTradeGsec_NewTradeByTradeGsecPortlet_INSTANCE_xbna"
_NS: Final = f"_{_PORTLET}_"
_SERVE_URL: Final = (
    f"{_PAGE}?p_p_id={_PORTLET}&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view"
    "&p_p_cacheability=cacheLevelPage&p_p_resource_id=serveResource"
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
# AES-128-ECB / PKCS7 key, hardcoded in CCIL's page JS (base64 "bXVzdGJlMTZieXRlc2tleQ==").
_AES_KEY: Final = b"mustbe16byteskey"


def encrypt_date(value: str) -> str:
    """Reproduce CCIL's ``encryptDetails``: AES-128-ECB/PKCS7 -> base64."""
    pad = padding.PKCS7(128).padder()
    data = pad.update(value.encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(_AES_KEY), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


class CcilHistoricalTradesSource(MetricsCollector):
    """Fetches CCIL NDS-OM historical trades for a date and aggregates them per ISIN."""

    name: Final = "ccil"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)
        self._primed = False

    def fetch_trades(self, as_of: dt.date) -> list[TradeRecord]:
        """Fetch + aggregate one day's NDS-OM trades (holidays return an empty list)."""
        self.reset_metrics()
        csv_text = self.download(as_of, as_of)
        records = aggregate_trades(csv_text, source=self.name)
        self.add_metric(
            as_of.isoformat(),
            bytes_downloaded=len(csv_text.encode()),
            rows_parsed=len(records),
        )
        return records

    def download(self, start: dt.date, end: dt.date) -> str:
        """Download the raw trade CSV for ``[start, end]`` (landing it), priming cookies once."""
        if not self._primed:
            self._client.get(_PAGE, headers=_PAGE_HEADERS)
            self._primed = True
        from_s, to_s = start.isoformat(), end.isoformat()
        response = self._client.post(
            _SERVE_URL,
            data={
                f"{_NS}fromDate1": from_s,
                f"{_NS}toDate1": to_s,
                f"{_NS}hidFrom": encrypt_date(from_s),
                f"{_NS}hidTo": encrypt_date(to_s),
            },
            headers=_POST_HEADERS,
        )
        text = response.text
        path = (
            self._settings.data_dir / "raw" / self.name / f"historical_trades_{from_s}_{to_s}.csv"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("ccil.hist_downloaded", start=from_s, end=to_s, bytes=len(text.encode()))
        return text


# ---------------------------------------------------------------------- parsing
def aggregate_trades(csv_text: str, *, source: str) -> list[TradeRecord]:
    """Parse the trade-by-trade CSV and aggregate to one :class:`TradeRecord` per ISIN per day."""
    if not csv_text.strip() or "ISIN" not in csv_text[:200]:
        return []
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader, None)
    if header is None:
        return []

    # (isin, date) -> aggregation accumulator
    groups: dict[tuple[str, dt.date], _Agg] = defaultdict(_Agg)
    for row in reader:
        parsed = _parse_row(row)
        if parsed is None:
            continue
        isin, trade_date, desc, face, price, ytm, time_s = parsed
        groups[(isin, trade_date)].add(desc, face, price, ytm, time_s)

    records = []
    for (isin, trade_date), agg in groups.items():
        records.append(agg.to_record(isin, trade_date, source))
    return records


class _Agg:
    """Volume-weighted accumulator for one ISIN's trades on one day."""

    __slots__ = (
        "count",
        "desc",
        "last_px",
        "last_time",
        "last_yld",
        "px_num",
        "total_face",
        "yld_num",
    )

    def __init__(self) -> None:
        self.desc: str | None = None
        self.count = 0
        self.total_face = 0.0
        self.px_num = 0.0
        self.yld_num = 0.0
        self.last_time = ""
        self.last_px: float | None = None
        self.last_yld: float | None = None

    def add(
        self, desc: str | None, face: float, price: float | None, ytm: float | None, time_s: str
    ) -> None:
        self.desc = self.desc or desc
        self.count += 1
        if price is not None and face > 0:
            self.total_face += face
            self.px_num += price * face
            if ytm is not None:
                self.yld_num += ytm * face
        if time_s >= self.last_time:  # latest trade of the day
            self.last_time, self.last_px, self.last_yld = time_s, price, ytm

    def to_record(self, isin: str, trade_date: dt.date, source: str) -> TradeRecord:
        wap = self.px_num / self.total_face if self.total_face else None
        way = self.yld_num / self.total_face if self.total_face else None
        return TradeRecord(
            isin=isin,
            trade_date=trade_date,
            source=source,
            segment=_instrument_segment(self.desc),
            descriptor=self.desc,
            ltp=self.last_px,
            lty=self.last_yld,
            no_of_trades=self.count,
            trade_value=self.total_face,
            wap=wap,
            way=way,
        )


def _parse_row(
    row: list[str],
) -> tuple[str, dt.date, str | None, float, float | None, float | None, str] | None:
    if len(row) < 8:
        return None
    trade_date = _as_date(row[0])
    isin = row[2].strip()
    if trade_date is None or len(isin) != 12 or not isin.startswith("IN"):
        return None
    return (
        isin,
        trade_date,
        row[3].strip() or None,
        _as_float(row[4]) or 0.0,
        _as_float(row[5]),
        _as_float(row[6]),
        row[1].strip(),
    )


def _instrument_segment(desc: str | None) -> str:
    """Classify an NDS-OM security description into an instrument segment.

    Naming seen in the feed: "GOVT. STOCK" (G-Sec), "DTB <date>" (Discounted Treasury Bill),
    "... SDL/SGS ..." (State Government Securities), "SGB ..." (Sovereign Gold Bond), "CMB".
    """
    up = (desc or "").upper()
    if "DTB" in up or "T-BILL" in up or "TREASURY BILL" in up or up.startswith("CMB"):
        return "TBILL"
    if "SGB" in up:
        return "SGB"
    if "SDL" in up or "SGS" in up:
        return "SDL"
    return "GSEC"


def _as_float(value: str) -> float | None:
    value = value.strip().replace(",", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_date(value: str) -> dt.date | None:
    value = value.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.UTC).date()
        except ValueError:
            continue
    return None
