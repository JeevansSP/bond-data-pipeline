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
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Final
from zipfile import BadZipFile

import httpx
import openpyxl
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.worksheet import Worksheet

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import InstrumentType, SovereignValuation, YieldCurvePoint
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import DataUnavailable, SourceError

logger = get_logger(__name__)

_BASE_URL: Final = "https://www.fbil.org.in/wasdm"
_REFERER: Final = "https://www.fbil.org.in/"

# Product -> instrument classification for the sovereign valuation datasets.
_PRODUCT_INSTRUMENT: Final[dict[str, InstrumentType]] = {
    "gsec": InstrumentType.GSEC,
    "sdl": InstrumentType.SDL,
    "strips": InstrumentType.STRIPS,
}

# Product -> (worksheet title, canonical curve key) for the published yield-curve sheets.
# The Par Yield curve rides inside the gsec workbook and the GOI ZCYC inside the strips
# workbook — they are extra sheets, not separate downloads. Titles are matched with
# spaces/hyphens/underscores stripped: old-format files say "Par-Yield", new say "Par Yield".
_PRODUCT_CURVE: Final[dict[str, tuple[str, str]]] = {
    "gsec": ("Par Yield", "gsec_par"),
    "strips": ("ZCYC", "gsec_zcyc"),
    "sdlzcyc": ("SDL_ZCYC", "sdl_zcyc"),
}

# Worksheet title (normalized) -> instrument, for workbooks that mix instrument types.
_SHEET_INSTRUMENT: Final[dict[str, InstrumentType]] = {
    "gsec": InstrumentType.GSEC,
    "special": InstrumentType.GSEC,  # GoI special securities (oil/FCI/recap bonds)
    "sdl": InstrumentType.SDL,
    "uday": InstrumentType.SDL,
    "strips": InstrumentType.STRIPS,
    "tbill": InstrumentType.TBILL,
}


def _normalize_title(title: str) -> str:
    """Normalize a sheet title for lookup: lowercase, spaces/hyphens/underscores stripped."""
    return title.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


class FbilSource(MetricsCollector):
    """Fetches and parses FBIL published sovereign valuation files."""

    name: Final = "fbil"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
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
            DataUnavailable: If FBIL has no file for that date (holiday/weekend -> HTTP 500,
                or a 200 with a non-xlsx body for dates outside the published range).
            SourceError: On any other HTTP *status* failure. Transport errors (timeouts,
                connection resets surviving the retry budget) propagate as ``httpx`` exceptions;
                the pipeline's ``execute_run`` records those as FAILED.
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
        if not content.startswith(b"PK\x03\x04"):
            # For dates outside the published range FBIL serves an HTML page with HTTP 200.
            # Don't land it as a mislabeled .xlsx in the data lake — treat as no-data.
            raise DataUnavailable(f"FBIL {product} returned a non-xlsx body for {date}")
        path = self._raw_path(product, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("fbil.downloaded", product=product, date=date.isoformat(), bytes=len(content))
        return content

    def read_or_download(self, product: str, date: dt.date) -> bytes:
        """Return the raw workbook, serving from the data lake when already landed.

        FBIL publishes one final file per business date, so a landed artifact never needs
        re-fetching — this makes lake-wide backfills (thousands of dates) network-free.
        Delete the raw file to force a re-download.
        """
        path = self._raw_path(product, date)
        if path.exists():
            return path.read_bytes()
        return self.download(product, date)

    # ------------------------------------------------------------------ parsing
    def fetch_valuations(self, product: str, date: dt.date) -> list[SovereignValuation]:
        """Download + parse one product/date into valuation records.

        Raises:
            ValueError: If ``product`` is not a supported sovereign valuation product.
        """
        instrument = _PRODUCT_INSTRUMENT.get(product)
        if instrument is None:
            raise ValueError(f"unsupported FBIL valuation product: {product!r}")
        self.reset_metrics()
        content = self.read_or_download(product, date)
        records, seen = self._parse_with_stats(content, date=date, instrument=instrument)
        self.add_metric(
            f"{product}/{date.isoformat()}",
            bytes_downloaded=len(content),
            rows_extracted=seen,
            rows_parsed=len(records),
            rows_dropped=seen - len(records),
        )
        return records

    def parse(
        self, content: bytes, *, date: dt.date, instrument: InstrumentType
    ) -> list[SovereignValuation]:
        """Parse a valuation workbook into records (see :meth:`_parse_with_stats`)."""
        records, _seen = self._parse_with_stats(content, date=date, instrument=instrument)
        return records

    def _parse_with_stats(
        self, content: bytes, *, date: dt.date, instrument: InstrumentType
    ) -> tuple[list[SovereignValuation], int]:
        """Parse a valuation workbook into records.

        The header row is located by content (the row whose first cell is ``ISIN``) so we are
        resilient to leading title/branding rows, rather than hard-coding row offsets.

        For dates outside its published range, FBIL serves an HTML page with HTTP 200 rather than a
        workbook; that isn't a valid xlsx, so it is treated as no-data (``DataUnavailable`` -> the
        pipeline records SKIPPED) rather than crashing a backfill.
        """
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except (BadZipFile, InvalidFileException) as exc:
            raise DataUnavailable(
                f"FBIL {instrument.value} returned a non-xlsx body for {date}"
            ) from exc
        try:
            records: list[SovereignValuation] = []
            seen = 0
            sheets = 0
            # Every ISIN-headed sheet is per-security data: the gsec workbook carries GoI
            # special securities (oil/FCI/recap bonds) on a "Special" sheet and the sdl
            # workbook carries UDAY bonds on a "UDAY" sheet alongside the main sheet.
            # Pre-Feb-2023 workbooks bundle *several* instrument types in one file
            # (G-Sec + SDL + Special), so each sheet's title picks its instrument; the
            # product's instrument is only the fallback for unrecognized titles.
            for title, rows, header_index, headers in _iter_data_sheets(workbook):
                sheets += 1
                sheet_instrument = _SHEET_INSTRUMENT.get(_normalize_title(title), instrument)
                columns = _column_map(headers)
                for raw in rows:  # iterator continues *after* the header row
                    seen += 1
                    record = _row_to_valuation(raw, columns, date, sheet_instrument, self.name)
                    if record is not None:
                        records.append(record)
                logger.debug(
                    "fbil.sheet_parsed",
                    sheet=title,
                    instrument=sheet_instrument.value,
                    date=date.isoformat(),
                    header_row=header_index,
                )
            if sheets == 0:
                raise SourceError("could not locate an 'ISIN' header row in any FBIL worksheet")
        finally:
            workbook.close()

        logger.info(
            "fbil.parsed",
            instrument=instrument.value,
            date=date.isoformat(),
            sheets=sheets,
            records=len(records),
            dropped=seen - len(records),
        )
        return records, seen

    # ------------------------------------------------------------------ curves
    def fetch_curves(self, product: str, date: dt.date) -> list[YieldCurvePoint]:
        """Fetch + parse one product/date's published yield-curve sheet.

        Raises:
            ValueError: If ``product`` has no curve sheet mapping.
        """
        if product not in _PRODUCT_CURVE:
            raise ValueError(f"unsupported FBIL curve product: {product!r}")
        self.reset_metrics()
        content = self.read_or_download(product, date)
        points = self.parse_curve(content, product=product, date=date)
        self.add_metric(
            f"{product}/{date.isoformat()}",
            bytes_downloaded=len(content),
            rows_extracted=len(points),
            rows_parsed=len(points),
            rows_dropped=0,
        )
        return points

    def parse_curve(self, content: bytes, *, product: str, date: dt.date) -> list[YieldCurvePoint]:
        """Parse the curve sheet (tenor / semi-annual / annualized) of a product workbook."""
        sheet_title, curve = _PRODUCT_CURVE[product]
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except (BadZipFile, InvalidFileException) as exc:
            raise DataUnavailable(f"FBIL {product} returned a non-xlsx body for {date}") from exc
        try:
            try:
                worksheet = _find_sheet(workbook, sheet_title)
            except SourceError as exc:
                # Pre-Feb-2023 workbooks use the old single-purpose format without curve
                # sheets — that's "no curve published for this date", not a broken file.
                raise DataUnavailable(
                    f"FBIL {product} workbook for {date} has no {sheet_title!r} sheet"
                ) from exc
            points: list[YieldCurvePoint] = []
            in_data = False
            for row in worksheet.iter_rows(values_only=True):
                first = row[0] if row else None
                if not in_data:
                    if isinstance(first, str) and first.strip().lower().startswith("tenor"):
                        in_data = True
                    continue
                tenor = _as_float(first)
                if tenor is None or tenor <= 0:
                    break  # blank/footer row = end of the tenor grid
                points.append(
                    YieldCurvePoint(
                        curve=curve,
                        quote_date=date,
                        tenor_years=tenor,
                        source=self.name,
                        ytm_semi_annual=_as_float(_cell(row, 1)),
                        ytm_annualized=_as_float(_cell(row, 2)),
                    )
                )
            if not in_data:
                raise SourceError(f"no 'Tenor' header row in FBIL sheet {sheet_title!r}")
        finally:
            workbook.close()
        logger.info("fbil.curve_parsed", curve=curve, date=date.isoformat(), points=len(points))
        return points


# ---------------------------------------------------------------------- helpers
def _find_header(rows: Iterable[Sequence[object]]) -> tuple[int, Sequence[object]]:
    """Advance ``rows`` to and return the ``(index, header_row)`` whose first cell is ISIN."""
    for index, row in enumerate(rows):
        first = row[0] if row else None
        if isinstance(first, str) and first.strip().upper() == "ISIN":
            return index, row
    raise SourceError("could not locate an 'ISIN' header row in FBIL worksheet")


def _iter_data_sheets(
    workbook: openpyxl.workbook.Workbook,
) -> Iterator[tuple[str, Iterator[Sequence[object]], int, Sequence[object]]]:
    """Yield every ISIN-headed worksheet as (title, row iterator, header index, header row).

    The iterator is positioned just after the header. All sheets are searched — FBIL ships
    per-security data across several tabs (main + Special/UDAY), and occasionally saves the
    workbook with a non-data sheet active (e.g. the "Note on FRB & IIB" tab), so neither
    ``workbook.active`` nor first-match can be trusted.
    """
    for worksheet in workbook.worksheets:
        rows: Iterator[Sequence[object]] = worksheet.iter_rows(values_only=True)
        try:
            index, headers = _find_header(rows)
        except SourceError:
            continue  # this sheet isn't a data sheet; try the next
        yield worksheet.title, rows, index, headers


def _find_sheet(workbook: openpyxl.workbook.Workbook, title: str) -> Worksheet:
    """Return the worksheet with the given title (case/space/hyphen-insensitive)."""
    wanted = _normalize_title(title)
    for worksheet in workbook.worksheets:
        if _normalize_title(worksheet.title) == wanted:
            return worksheet
    raise SourceError(f"FBIL workbook has no sheet titled {title!r}")


def _column_map(headers: Sequence[object]) -> dict[str, int]:
    """Map logical field names to column indices by matching header prefixes."""
    matchers: dict[str, Callable[[str], bool]] = {
        "isin": lambda h: h == "isin",
        "description": lambda h: h.startswith("description"),
        "coupon": lambda h: h.startswith("coupon"),
        "maturity": lambda h: h.startswith("maturity"),
        "price": lambda h: h.startswith("price"),
        # The STRIPS sheet titles its yield column "Yield% (Semi-Annual)" rather than "YTM…".
        "ytm": lambda h: h.startswith(("ytm", "yield")),
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
