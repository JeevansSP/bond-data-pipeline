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

Note on units: ``trade_value`` here is the summed **face value** traded (turnover in face terms),
a different basis from NSE's session turnover — don't sum ``trades.trade_value`` across sources.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import InstrumentType, SecurityRecord, TradeRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import DataUnavailable, SourceError
from bonds.states import sdl_issuer

logger = get_logger(__name__)

# One parsed trade row: (isin, trade_date, description, face, price, ytm, time_key).
_TimeKey = tuple[int, int, int]
_ParsedRow = tuple[str, dt.date, str | None, float, float | None, float | None, _TimeKey]

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
        """Fetch + aggregate one day's NDS-OM trades.

        Raises:
            DataUnavailable: When the CSV carries no trade rows — a holiday, or (the common
                case) a run that fired before CCIL published the day's file. Either way the day
                is *not yet ingested*, so it must be recorded SKIPPED and re-attempted, never
                SUCCESS with zero rows: a zero-row success advances the catch-up anchor past the
                date and the data is lost for good. That is exactly how three weeks of the
                sovereign tape went missing behind 17 consecutive "success" audit rows after the
                scheduler drifted to a 13:00 run (CCIL publishes after the 17:00 close).
            SourceError: When the CSV has rows but none parse — a layout change, which must fail
                loudly rather than masquerade as an empty day.
        """
        self.reset_metrics()
        csv_text = self.download(as_of, as_of)
        result = aggregate_trades_with_stats(csv_text, source=self.name)
        if result.rows_seen == 0:
            raise DataUnavailable(f"CCIL published no trades for {as_of} (holiday or not yet up)")
        if result.rows_used == 0:
            raise SourceError(
                f"CCIL returned {result.rows_seen} rows for {as_of} but none parsed (layout?)"
            )
        dropped = result.rows_seen - result.rows_used
        if dropped:
            # A layout change breaking a fraction of rows would silently skew VWAPs otherwise.
            logger.warning(
                "ccil.hist_rows_dropped",
                as_of=as_of.isoformat(),
                dropped=dropped,
                seen=result.rows_seen,
            )
        self.add_metric(
            as_of.isoformat(),
            bytes_downloaded=len(csv_text.encode()),
            rows_extracted=result.rows_seen,
            rows_parsed=result.rows_used,
            rows_dropped=dropped,
        )
        return result.records

    def download(self, start: dt.date, end: dt.date) -> str:
        """Download the raw trade CSV for ``[start, end]`` (landing it).

        Akamai/Liferay return challenge/error pages as HTTP 200 with HTML. If we get one — e.g.
        cookies expired mid-backfill — we re-prime cookies and retry once; a persistent non-CSV
        response raises :class:`SourceError` (→ audited FAILED) rather than being silently treated
        as an empty (holiday) day.
        """
        self._prime()
        text = self._post(start, end)
        if _looks_like_html(text):
            self._primed = False
            self._prime()
            text = self._post(start, end)
            if _looks_like_html(text):
                raise SourceError(f"CCIL returned a non-CSV page for {start}..{end} (challenge?)")
        from_s, to_s = start.isoformat(), end.isoformat()
        path = (
            self._settings.data_dir / "raw" / self.name / f"historical_trades_{from_s}_{to_s}.csv"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("ccil.hist_downloaded", start=from_s, end=to_s, bytes=len(text.encode()))
        return text

    def _prime(self) -> None:
        if not self._primed:
            self._client.get(_PAGE, headers=_PAGE_HEADERS)  # mint Akamai cookies
            self._primed = True

    def _post(self, start: dt.date, end: dt.date) -> str:
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
        return response.text


# ---------------------------------------------------------------------- parsing
@dataclass(frozen=True, slots=True)
class AggregatedTrades:
    """Aggregated records plus raw-row funnel counts for one CSV."""

    records: list[TradeRecord]
    rows_seen: int
    """Data rows in the CSV (header excluded)."""
    rows_used: int
    """Rows that parsed into trades (the rest were malformed and dropped)."""


def aggregate_trades(csv_text: str, *, source: str) -> list[TradeRecord]:
    """Parse the trade-by-trade CSV and aggregate to one :class:`TradeRecord` per ISIN per day."""
    return aggregate_trades_with_stats(csv_text, source=source).records


def aggregate_trades_with_stats(csv_text: str, *, source: str) -> AggregatedTrades:
    """As :func:`aggregate_trades`, also counting raw vs dropped rows for the metrics funnel."""
    if not csv_text.strip():
        return AggregatedTrades(records=[], rows_seen=0, rows_used=0)
    reader = csv.reader(io.StringIO(csv_text))
    next(reader, None)  # drop the header row (columns validated positionally in _parse_row)

    # (isin, date) -> aggregation accumulator
    groups: dict[tuple[str, dt.date], _Agg] = defaultdict(_Agg)
    seen = used = 0
    for row in reader:
        if not row:
            continue
        seen += 1
        parsed = _parse_row(row)
        if parsed is None:
            continue
        used += 1
        isin, trade_date, desc, face, price, ytm, time_key = parsed
        groups[(isin, trade_date)].add(desc, face, price, ytm, time_key)

    records = []
    for (isin, trade_date), agg in groups.items():
        records.append(agg.to_record(isin, trade_date, source))
    return AggregatedTrades(records=records, rows_seen=seen, rows_used=used)


class _Agg:
    """Volume-weighted accumulator for one ISIN's trades on one day."""

    __slots__ = (
        "count",
        "desc",
        "last_key",
        "last_px",
        "last_yld",
        "px_num",
        "total_face",
        "yld_face",
        "yld_num",
    )

    def __init__(self) -> None:
        self.desc: str | None = None
        self.count = 0
        self.total_face = 0.0
        self.px_num = 0.0
        self.yld_face = 0.0
        self.yld_num = 0.0
        self.last_key: _TimeKey = (-1, -1, -1)
        self.last_px: float | None = None
        self.last_yld: float | None = None

    def add(
        self,
        desc: str | None,
        face: float,
        price: float | None,
        ytm: float | None,
        time_key: _TimeKey,
    ) -> None:
        self.desc = self.desc or desc
        self.count += 1  # every trade counts toward no_of_trades
        if price is not None and price > 0 and face > 0:
            self.total_face += face
            self.px_num += price * face
            if ytm is not None:
                # Yield needs its own denominator: a priced trade with a blank/unparseable
                # yield must not dilute the weighted yield toward zero.
                self.yld_face += face
                self.yld_num += ytm * face
            if time_key >= self.last_key:  # ltp/lty = latest trade with a valid price
                self.last_key, self.last_px, self.last_yld = time_key, price, ytm
        elif ytm is not None and ytm != 0 and face > 0:
            # Yield-only print (e.g. a when-issued quote whose "price" cell held the yield and
            # was nulled by the repair): no price legs, but it still informs the weighted yield.
            self.yld_face += face
            self.yld_num += ytm * face

    def to_record(self, isin: str, trade_date: dt.date, source: str) -> TradeRecord:
        wap = self.px_num / self.total_face if self.total_face else None
        way = self.yld_num / self.yld_face if self.yld_face else None
        return TradeRecord(
            isin=isin,
            trade_date=trade_date,
            source=source,
            segment=_instrument_segment(self.desc, isin),
            descriptor=self.desc,
            ltp=self.last_px,
            lty=self.last_yld,
            no_of_trades=self.count,
            trade_value=self.total_face,
            wap=wap,
            way=way,
        )


# Plausibility bounds for Indian sovereign quotes. Yields above _YTM_IMPLAUSIBLE are never real
# (historical repo-era peaks sit well under 20); yields in [_YTM_MIN, 0) are rare but genuine
# (capital-indexed bonds above redemption printed ~-16.7% in 2002). Prices below
# _PRICE_MIN_PAR_SEGMENT only occur for deep-discount STRIPS — for every other segment a value
# that low in the price column is a yield, not a price (verified against 15.7M landed rows).
_YTM_IMPLAUSIBLE: Final = 40.0
_YTM_MIN: Final = -20.0
_PRICE_MIN_PAR_SEGMENT: Final = 40.0


def _repair_price_yield(
    price: float | None, ytm: float | None, *, deep_discount: bool
) -> tuple[float | None, float | None]:
    """Undo CCIL's occasional price/yield column garbling; discard what can't be trusted.

    Verified against all occurrences in 15.7M landed rows:

    * yield implausible and price yield-sized -> a clean transposition: swap back (all 68 real
      cases genuine, including deep-discount STRIPS at price 6.15 / "yield" 76.35);
    * yield implausible and price price-sized -> BOTH cells are garbled (in every landed case
      the "plausible" price was also wrong, e.g. a 91-day T-bill at 86.41 with the true price
      98.94 sitting in the yield column) -> drop both rather than record a confidently-wrong
      price;
    * non-deep-discount segment with a yield-sized price (e.g. when-issued G-Secs quoted
      price=7.25 / ytm=0.0) -> the price column holds a yield: drop the price, keep the more
      plausible yield leg;
    * yield below ``_YTM_MIN`` -> drop the yield (keep genuine capital-indexed negatives).

    ``deep_discount`` (STRIPS) exempts the low-price rule: a 2040 principal strip legitimately
    trades at 7.79.
    """
    if ytm is not None and ytm > _YTM_IMPLAUSIBLE:
        if price is not None and 0 < price < _PRICE_MIN_PAR_SEGMENT:
            return ytm, price
        return None, None
    if not deep_discount and price is not None and 0 < price < _PRICE_MIN_PAR_SEGMENT:
        yield_leg = (
            ytm if ytm is not None and _YTM_MIN <= ytm <= _YTM_IMPLAUSIBLE and ytm != 0 else price
        )
        return None, yield_leg
    if ytm is not None and ytm < _YTM_MIN:
        return price, None
    return price, ytm


def _parse_row(row: list[str]) -> _ParsedRow | None:
    # Reject any row whose column count isn't the expected 8 — an unquoted comma (in a description
    # or a grouped number like "7,50,00,000") would otherwise shift every field silently.
    if len(row) != 8:
        return None
    trade_date = _as_date(row[0])
    isin = row[2].strip()
    if trade_date is None or len(isin) != 12 or not isin.startswith("IN"):
        return None
    desc = row[3].strip() or None
    segment = _instrument_segment(desc, isin)
    price, ytm = _repair_price_yield(
        _as_float(row[5]), _as_float(row[6]), deep_discount=segment == "STRIPS"
    )
    return (
        isin,
        trade_date,
        desc,
        _as_float(row[4]) or 0.0,
        price,
        ytm,
        _time_key(row[1]),
    )


# STRIPS trade as "GOVT. STOCK <DDMMMYYYY>C|P" — a single cashflow date with a C (coupon strip)
# or P (principal strip) suffix, vs a regular G-Sec ending in a 4-digit year. The optional space
# accommodates the older "GOVT. STOCK 02JAN2024 C" spelling alongside "...2024C".
_STRIP_RE: Final = re.compile(r"\d{2}[A-Z]{3}\d{4}\s?[CP]$")


def _instrument_segment(desc: str | None, isin: str) -> str:
    """Classify an NDS-OM trade into an instrument segment.

    The **issuer** is taken authoritatively from the ISIN, not the free-text description: central
    government securities are ``IN00...`` while every other prefix is a state issuer (SDL). This is
    stable across 24 years of naming drift — the same state loan appears as "MAHARASHTRA S.D.",
    then "... SDL", then "... SGS", and old T-bills as "91 TBILL" vs "091 DTB" — which description
    parsing alone misclassifies (verified: the ISIN prefix split matches 15M+ rows with no leakage).

    Within central government, the description distinguishes the sub-type: "DTB"/"TBILL"/"CMB"
    (Treasury Bill), "SGB" (Sovereign Gold Bond), STRIPS ("... 12DEC2041C"), else a coupon G-Sec.
    """
    if len(isin) >= 4 and isin[2:4] != "00":
        return "SDL"  # state issuer -> State Development Loan
    up = (desc or "").strip().upper()
    if (
        "DTB" in up
        or "TBILL" in up
        or "T-BILL" in up
        or "TREASURY BILL" in up
        or up.startswith("CMB")
    ):
        return "TBILL"
    if "SGB" in up:
        return "SGB"
    if _STRIP_RE.search(up):
        return "STRIPS"
    return "GSEC"


# ---------------------------------------------------------- securities-master derivation
# CCIL is the only source for T-Bills, STRIPS, SGBs and many matured/historical G-Secs and SDLs,
# so we derive a reference `securities` row from each traded ISIN (best-effort maturity/coupon
# parsed from the description) to fill gaps the FBIL/BondCentral universe never covers.
_SEGMENT_TYPE: Final = {
    "GSEC": InstrumentType.GSEC,
    "SDL": InstrumentType.SDL,
    "TBILL": InstrumentType.TBILL,
    "STRIPS": InstrumentType.STRIPS,
    "SGB": InstrumentType.SGB,
}
_CENTRAL_SEGMENTS: Final = frozenset({"GSEC", "TBILL", "STRIPS", "SGB"})
_ZERO_COUPON_SEGMENTS: Final = frozenset({"TBILL", "STRIPS"})
# Sovereign convention: Rs 100 face value (same constant FBIL's valuation path uses). SGB is
# excluded — its face is the gold-linked issue price (e.g. "... FV 4791"), not in this feed.
_PAR_FACE_SEGMENTS: Final = frozenset({"GSEC", "SDL", "TBILL", "STRIPS"})
_SOVEREIGN_FACE_VALUE: Final = 100.0
_LEAD_COUPON_RE: Final = re.compile(r"^\s*(\d{1,2}\.\d{1,3})\b")
_SLASH_DATE_RE: Final = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")  # DD/MM/YYYY
_COMPACT_DATE_RE: Final = re.compile(r"\b(\d{2})(\d{2})(\d{4})\b")  # DDMMYYYY, e.g. DTB 15012027
# DDMMMYYYY, e.g. "12DEC2041" — no leading \b so it also matches the glued "GS02JAN2011C" /
# "GS15JUN2049P" strip spellings (a \b fails between the S and the digit, both word chars).
_DMMMY_RE: Final = re.compile(r"(\d{2})([A-Z]{3})(\d{4})")
_MONTHS: Final = {
    m: i
    for i, m in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
    )
}


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _parse_maturity(desc: str | None, segment: str) -> dt.date | None:
    """Best-effort maturity from a CCIL description; only exact dates (not year-only)."""
    up = (desc or "").upper()
    if segment == "TBILL":  # "091 DTB MATURING 07/06/2002" or "DTB 15012027"
        m = _SLASH_DATE_RE.search(up) or _COMPACT_DATE_RE.search(up)
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
    for m in _DMMMY_RE.finditer(up):  # STRIPS / dated stock: "12DEC2041"; first valid-month wins
        if m.group(2) in _MONTHS:
            return _safe_date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    return None  # G-Sec/SDL/SGB carry only a maturity year in the feed -> left unknown


def _parse_coupon(desc: str | None, segment: str) -> float | None:
    if segment in _ZERO_COUPON_SEGMENTS:
        return 0.0
    m = _LEAD_COUPON_RE.match((desc or "").strip())
    return float(m.group(1)) if m else None


# The state vocabulary and description parsing live in bonds.states, shared with the FBIL
# valuation pipeline: when each connector kept its own, CCIL normalised to full state names
# while FBIL emitted raw two-letter codes and the master fragmented into 63 issuer strings.


def derive_security(isin: str, descriptor: str | None, segment: str) -> SecurityRecord | None:
    """Build a reference :class:`SecurityRecord` from one traded CCIL instrument."""
    itype = _SEGMENT_TYPE.get(segment)
    if itype is None:
        return None
    if segment in _CENTRAL_SEGMENTS:
        issuer: str | None = "Government of India"
    else:
        issuer = sdl_issuer(descriptor)  # SDL: state parsed from the description
    return SecurityRecord(
        isin=isin,
        instrument_type=itype,
        source="ccil",
        description=descriptor,
        issuer=issuer,
        coupon=_parse_coupon(descriptor, segment),
        interest_type="ZERO_COUPON" if segment in _ZERO_COUPON_SEGMENTS else "FIXED",
        maturity_date=_parse_maturity(descriptor, segment),
        face_value=_SOVEREIGN_FACE_VALUE if segment in _PAR_FACE_SEGMENTS else None,
    )


def derive_securities(trades: list[TradeRecord]) -> list[SecurityRecord]:
    """Reference securities for a batch of CCIL trades (one per ISIN, last descriptor wins)."""
    by_isin: dict[str, SecurityRecord] = {}
    for t in trades:
        sec = derive_security(t.isin, t.descriptor, t.segment)
        if sec is not None:
            by_isin[t.isin] = sec
    return list(by_isin.values())


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


def _time_key(value: str) -> tuple[int, int, int]:
    """Parse an ``HH:MM:SS`` trade time into a numeric sort key.

    Integer comparison is padding-agnostic — ``"9:05:00"`` and ``"09:05:00"`` both sort correctly —
    unlike lexical string comparison, which would rank an unpadded 9 AM after 4 PM. Unparseable
    times sort earliest, so they lose to any parseable time; among equal keys (including a group
    where every time is unparseable) the later row in file order wins, so a group still gets an
    ``ltp`` rather than none.
    """
    parts = value.strip().split(":")
    if len(parts) != 3:
        return (-1, -1, -1)
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return (-1, -1, -1)


def _looks_like_html(text: str) -> bool:
    """True if the payload is an HTML challenge/error page rather than the trade CSV."""
    head = text[:512].lstrip().lower()
    return head.startswith(("<!doctype", "<html")) or "<body" in head
