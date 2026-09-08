"""BSE corporate-bond Trade & Settlement source (trade-level, RFQ + OTC-reported).

    GET https://api.bseindia.com/BseIndiaAPI/api/rdbTradensettle/w?frmDate=YYYYMMDD&toDate=YYYYMMDD
    -> {"Table": [one object per transaction]}

No cookies/captcha — just a browser User-Agent and a bseindia.com Referer. One row per
transaction with second-resolution ``Trade_Time`` and an RFQ-vs-OTC flag. Trade-level history
starts ~25-Nov-2020 (SEBI's Oct-2020 dissemination circular); earlier dates return zero rows.
Empty days (holidays) are ``DataUnavailable`` so pipelines record SKIPPED.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Final

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import CorporateTradeRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import DataUnavailable, SourceError

logger = get_logger(__name__)

_API: Final = "https://api.bseindia.com/BseIndiaAPI/api/rdbTradensettle/w"
_HEADERS: Final = {"Accept": "application/json", "Referer": "https://www.bseindia.com/"}

# Yield types whose YieldDate is an embedded option exercise date rather than maturity.
_OPTION_YIELD_TYPES: Final = frozenset({"YTC", "YTP"})


class BseSource(MetricsCollector):
    """Fetches and parses BSE corporate-bond trade & settlement data."""

    name: Final = "bse"

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
            / self.name
            / "tradensettle"
            / f"{date.isoformat()}.json"
        )

    def download(self, date: dt.date) -> bytes:
        """Download one day's trades, landing a copy in the data lake.

        Raises:
            DataUnavailable: When BSE answers with an empty ``Table`` — a holiday, or a run that
                fired before the day's trades were published. Such a body is *not* landed: it is
                syntactically valid JSON, so landing it would make ``read_or_download`` serve it
                back on every later retry and permanently cache "no data" for that date. That is
                how three weeks of BSE trades stayed missing while the audit trail dutifully
                recorded a skip each night — the retries were all reading a 12-byte lake file.

                A body that is not JSON at all is a different failure and is landed as usual, so
                ``parse`` can report it and the artifact survives for diagnosis.
        """
        stamp = date.strftime("%Y%m%d")
        response = self._client.get(
            _API, params={"frmDate": stamp, "toDate": stamp}, headers=_HEADERS
        )
        content = response.content
        if _is_empty_table(content):
            raise DataUnavailable(f"BSE published no corporate trades for {date}")
        path = self._raw_path(date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("bse.downloaded", date=date.isoformat(), bytes=len(content))
        return content

    def read_or_download(self, date: dt.date) -> bytes:
        """Return the raw payload, serving from the data lake when already landed.

        A landed artifact carrying no rows is treated as a cache *miss*, not as data: earlier
        runs (before ``download`` learnt to reject them) poisoned the lake with empty-``Table``
        bodies, and serving those back is what stopped every nightly retry from healing the gap.
        """
        path = self._raw_path(date)
        if path.exists():
            # One read, not two: a landed day is a multi-MB file and the backfill loop hits
            # this once per business day.
            landed = path.read_bytes()
            if not _is_empty_table(landed):
                return landed
        return self.download(date)

    # ------------------------------------------------------------------ parsing
    def fetch_trades(self, date: dt.date) -> list[CorporateTradeRecord]:
        """Fetch + parse one day's trade-level records.

        Raises:
            DataUnavailable: When BSE published no trades for the date (holiday/weekend, a date
                before the feed's late-2020 start, or a run before publication).
            SourceError: When rows were returned but none parsed — a layout change.
        """
        self.reset_metrics()
        content = self.read_or_download(date)
        records, seen = self.parse(content, date=date)
        self.add_metric(
            f"tradensettle/{date.isoformat()}",
            bytes_downloaded=len(content),
            rows_extracted=seen,
            rows_parsed=len(records),
            rows_dropped=seen - len(records),
        )
        if not records:
            # download() already rejects an empty Table, so rows existed and none parsed:
            # a layout change, which must fail loudly rather than look like a holiday.
            raise SourceError(f"BSE returned {seen} rows for {date} but none parsed (layout?)")
        return records

    def parse(self, content: bytes, *, date: dt.date) -> tuple[list[CorporateTradeRecord], int]:
        """Parse a raw payload into records; returns ``(records, rows_seen)``."""
        try:
            rows: list[dict[str, Any]] = json.loads(content).get("Table") or []
        except (json.JSONDecodeError, AttributeError) as exc:
            raise DataUnavailable(f"BSE returned a non-JSON body for {date}") from exc
        records = []
        for row in rows:
            record = _to_record(row, date, self.name)
            if record is not None:
                records.append(record)
        logger.info(
            "bse.parsed",
            date=date.isoformat(),
            records=len(records),
            dropped=len(rows) - len(records),
        )
        return records, len(rows)


def _is_empty_table(content: bytes) -> bool:
    """Whether a raw BSE payload is valid JSON carrying an empty ``Table``.

    Deliberately narrower than "has no rows": a body that is not JSON at all — a WAF challenge
    page, a truncated response — is **not** an empty day. Conflating the two would report an
    extended BSE block as a run of holidays and, worse, discard the artifact that shows what
    actually came back. Non-JSON therefore falls through to be landed and reported by
    :meth:`BseSource.parse`.
    """
    try:
        return not json.loads(content).get("Table")
    except (json.JSONDecodeError, AttributeError):
        return False


def _to_record(row: dict[str, Any], date: dt.date, source: str) -> CorporateTradeRecord | None:
    isin = str(row.get("ISIN") or "").strip()
    if len(isin) != 12 or not isin.startswith("IN"):
        return None
    trade_time = _as_datetime(row.get("Trade_Time"))
    yield_type = _as_str(row.get("Yield_Type"))
    yield_date = _as_datetime(row.get("YieldDate"))
    return CorporateTradeRecord(
        isin=isin,
        trade_date=trade_time.date() if trade_time else date,
        source=source,
        trade_time=trade_time,
        listed=_as_str(row.get("IsListed")),
        deal_type=_as_str(row.get("DealType")),
        issuer=_as_str(row.get("Issuer_Name")),
        description=_as_str(row.get("IssueDescription")),
        coupon=_as_float(row.get("CouponRate")),
        price=_as_float(row.get("Trade_Price")),
        trade_yield=_as_float(row.get("Trade_Yield")),
        yield_type=yield_type,
        outside_yield_range=_as_str(row.get("OutSideYieldRange")),
        # YieldDate is the option exercise date only for yield-to-call/put trades;
        # for YTM it is just maturity, which the securities master already carries.
        put_call_date=(
            yield_date.date() if yield_date and yield_type in _OPTION_YIELD_TYPES else None
        ),
        trade_value_lakh=_as_float(row.get("Trade_Value")),
        settlement_date=_parse_settlement_date(row.get("Settlement_Date")),
        settlement_status=_as_str(row.get("OrderStatus")),
        venue=_as_str(row.get("RFQReported")),
    )


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_datetime(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_settlement_date(value: object) -> dt.date | None:
    """BSE settlement dates look like ``27 Jul 2026``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.strptime(value.strip(), "%d %b %Y").date()
    except ValueError:
        return None
