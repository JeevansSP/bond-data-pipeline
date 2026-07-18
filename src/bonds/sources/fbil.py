"""FBIL connector — sovereign valuation price/yield (the price-history engine).

Endpoint (no auth; needs a browser UA + ``Accept`` + ``Referer``)::

    GET https://www.fbil.org.in/wasdm/<product>/downloadPublished?date=YYYY-MM-DD  -> .xlsx

Confirmed products & per-security schemas (see docs/research/2026-07-18_120554_fbil.org.in.md):
    gsec : ISIN, Coupon, Maturity(dd-mmm-yyyy), Price(Rs), YTM% p.a. (Semi-Annual), Remark 1, 2
    sdl  : ISIN, Description, Coupon, Maturity, Price(Rs), YTM% p.a. (Semi-Annual)

Non-publishing days (weekends/holidays) return HTTP 500 -> :class:`DataUnavailable`.
"""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Final

import httpx
import openpyxl

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import InstrumentType, SovereignValuation
from bonds.sources.base import DataUnavailable, SourceError

logger = get_logger(__name__)

_BASE_URL: Final = "https://www.fbil.org.in/wasdm"
_REFERER: Final = "https://www.fbil.org.in/"

# Product -> instrument classification for the sovereign valuation datasets.
_PRODUCT_INSTRUMENT: Final[dict[str, InstrumentType]] = {
    "gsec": InstrumentType.GSEC,
    "sdl": InstrumentType.SDL,
}


class FbilSource:
    """Fetches and parses FBIL published sovereign valuation files."""

    name: Final = "fbil"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    # ------------------------------------------------------------------ raw fetch
    def _raw_path(self, product: str, date: dt.date) -> Path:
        return self._settings.data_dir / "raw" / self.name / product / f"{date.isoformat()}.xlsx"

    def download(self, product: str, date: dt.date) -> bytes:
        """Download the published valuation workbook, landing a copy in the data lake.

        Args:
            product: FBIL product key (e.g. ``"gsec"`` or ``"sdl"``).
            date: Business date to fetch.

        Returns:
            Raw ``.xlsx`` bytes.

        Raises:
            DataUnavailable: If FBIL has no file for that date (holiday/weekend -> HTTP 500).
            SourceError: On any other HTTP failure.
        """
        url = f"{_BASE_URL}/{product}/downloadPublished"
        try:
            response = self._client.get(
                url,
                params={"date": date.isoformat()},
                headers={"Accept": "*/*", "Referer": _REFERER},
                # 500 = non-publishing day (expected); don't burn the retry/backoff budget on it.
                no_retry_statuses=frozenset({httpx.codes.INTERNAL_SERVER_ERROR}),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.INTERNAL_SERVER_ERROR:
                raise DataUnavailable(f"FBIL {product} has no data for {date}") from exc
            raise SourceError(f"FBIL {product} download failed for {date}: {exc}") from exc

        content = response.content
        path = self._raw_path(product, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("fbil.downloaded", product=product, date=date.isoformat(), bytes=len(content))
        return content

    # ------------------------------------------------------------------ parsing
    def fetch_valuations(self, product: str, date: dt.date) -> list[SovereignValuation]:
        """Download + parse one product/date into valuation records.

        Raises:
            ValueError: If ``product`` is not a supported sovereign valuation product.
        """
        instrument = _PRODUCT_INSTRUMENT.get(product)
        if instrument is None:
            raise ValueError(f"unsupported FBIL valuation product: {product!r}")
        content = self.download(product, date)
        return self.parse(content, date=date, instrument=instrument)

    def parse(
        self, content: bytes, *, date: dt.date, instrument: InstrumentType
    ) -> list[SovereignValuation]:
        """Parse a valuation workbook into records.

        The header row is located by content (the row whose first cell is ``ISIN``) so we are
        resilient to leading title/branding rows, rather than hard-coding row offsets.
        """
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            rows: Iterable[Sequence[object]] = workbook.active.iter_rows(values_only=True)
            header_index, headers = _find_header(rows)
            columns = _column_map(headers)
            records: list[SovereignValuation] = []
            seen = 0
            for raw in rows:  # iterator continues *after* the header row
                seen += 1
                record = _row_to_valuation(raw, columns, date, instrument, self.name)
                if record is not None:
                    records.append(record)
        finally:
            workbook.close()

        logger.info(
            "fbil.parsed",
            instrument=instrument.value,
            date=date.isoformat(),
            header_row=header_index,
            records=len(records),
            dropped=seen - len(records),
        )
        return records


# ---------------------------------------------------------------------- helpers
def _find_header(rows: Iterable[Sequence[object]]) -> tuple[int, Sequence[object]]:
    """Advance ``rows`` to and return the ``(index, header_row)`` whose first cell is ISIN."""
    for index, row in enumerate(rows):
        first = row[0] if row else None
        if isinstance(first, str) and first.strip().upper() == "ISIN":
            return index, row
    raise SourceError("could not locate an 'ISIN' header row in FBIL workbook")


def _column_map(headers: Sequence[object]) -> dict[str, int]:
    """Map logical field names to column indices by matching header prefixes."""
    matchers: dict[str, Callable[[str], bool]] = {
        "isin": lambda h: h == "isin",
        "description": lambda h: h.startswith("description"),
        "coupon": lambda h: h.startswith("coupon"),
        "maturity": lambda h: h.startswith("maturity"),
        "price": lambda h: h.startswith("price"),
        "ytm": lambda h: h.startswith("ytm"),
    }
    result: dict[str, int] = {}
    for index, cell in enumerate(headers):
        if not isinstance(cell, str):
            continue
        normalized = cell.strip().lower()
        for field, matches in matchers.items():
            if field not in result and matches(normalized):
                result[field] = index
    if "isin" not in result:
        raise SourceError("FBIL header row missing an ISIN column")
    return result


def _row_to_valuation(
    row: Sequence[object],
    columns: dict[str, int],
    date: dt.date,
    instrument: InstrumentType,
    source: str,
) -> SovereignValuation | None:
    """Convert a data row to a valuation, or ``None`` if it is not a valid security row."""
    isin = _cell(row, columns.get("isin"))
    if not isinstance(isin, str) or len(isin.strip()) != 12 or not isin.strip().startswith("IN"):
        return None
    return SovereignValuation(
        isin=isin.strip(),
        quote_date=date,
        instrument_type=instrument,
        source=source,
        description=_as_str(_cell(row, columns.get("description"))),
        coupon=_as_float(_cell(row, columns.get("coupon"))),
        maturity_date=_as_date(_cell(row, columns.get("maturity"))),
        price=_as_float(_cell(row, columns.get("price"))),
        ytm=_as_float(_cell(row, columns.get("ytm"))),
    )


def _cell(row: Sequence[object], index: int | None) -> object:
    if index is None or index >= len(row):
        return None
    return row[index]


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(value.strip(), fmt).replace(tzinfo=dt.UTC).date()
            except ValueError:
                continue
    return None
