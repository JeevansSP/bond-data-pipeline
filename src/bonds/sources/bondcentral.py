"""BondCentral connector — corporate securities-master universe (pillar 1).

Endpoint (no auth, open CORS): ``GET https://api.bondcentral.in/securities/?page=&size=`` with
``size`` capped at 100 (~25,501 ISINs across ~256 pages). Each item is ``{"isin", "data": {...}}``
with ~60 reference fields. See docs/research/2026-07-18_112508_bondcentral.in.md.

Every security is classified :data:`InstrumentType.CORP`. The credit rating (from ``data.ratings``)
is surfaced as a trackable attribute so the universe pipeline can record rating changes (SCD-2).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import httpx

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import InstrumentType, SecurityRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import SourceError

logger = get_logger(__name__)

_URL: Final = "https://api.bondcentral.in/securities/"
_ORIGIN: Final = "https://bondcentral.in"
_MAX_PAGE_SIZE: Final = 100
# BondCentral persistently 500s on several broken ~100-record windows (~6 of 256 pages observed,
# ~2.4%); those records are simply un-fetchable, so skip them and take the ~25k we can get rather
# than sink the whole snapshot. The cap still trips a genuine outage (>~12% of pages failing).
_MAX_SKIPPED_PAGES: Final = 30


class BondCentralSource(MetricsCollector):
    """Paginates the BondCentral securities master into :class:`SecurityRecord` objects."""

    name: Final = "bondcentral"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    def _raw_path(self, as_of: dt.date, page: int) -> Path:
        return (
            self._settings.data_dir
            / "raw"
            / self.name
            / as_of.isoformat()
            / f"page_{page:04d}.json"
        )

    def _fetch_page(self, page: int, size: int, as_of: dt.date) -> tuple[dict[str, Any], int]:
        """Fetch one page (returns ``(payload, bytes)``), landing the raw JSON in the data lake."""
        response = self._client.get(
            _URL,
            params={"page": str(page), "size": str(size)},
            headers={"Accept": "application/json", "Origin": _ORIGIN},
        )
        payload: dict[str, Any] = response.json()  # raises ValueError on a non-JSON 200
        if not isinstance(payload, dict):
            raise ValueError(f"page {page}: non-object JSON payload")
        path = self._raw_path(as_of, page)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload, len(response.content)

    def iter_records(
        self, as_of: dt.date, *, size: int = _MAX_PAGE_SIZE, max_pages: int | None = None
    ) -> Iterator[SecurityRecord]:
        """Yield every universe security, paging until exhausted (or ``max_pages``).

        Args:
            as_of: Snapshot date (used for the data-lake path and audit).
            size: Page size (clamped to the API max of 100).
            max_pages: Optional cap on pages fetched (useful for smoke runs/tests).

        Yields:
            One :class:`SecurityRecord` per security.

        Note:
            This is a generator: funnel metrics are recorded on exhaustion, so callers must
            consume it fully (the universe pipeline materialises it with ``list()``).
        """
        size = min(size, _MAX_PAGE_SIZE)
        self.reset_metrics()
        page = 1
        total_bytes = total_items = total_kept = skipped_pages = consecutive_failures = 0
        known_total_pages: int | None = None
        while True:
            try:
                payload, page_bytes = self._fetch_page(page, size, as_of)
            except (httpx.HTTPError, ValueError) as exc:
                # A persistently-failing page — an HTTP error OR a 200 with a non-JSON/malformed
                # body (proxy error page) — must not abort the whole snapshot: skip it and go on,
                # capped so a genuine outage still fails loudly.
                skipped_pages += 1
                consecutive_failures += 1
                logger.warning("bondcentral.page_skipped", page=page, error=str(exc)[:80])
                if skipped_pages > _MAX_SKIPPED_PAGES:
                    raise SourceError(
                        f"BondCentral: {skipped_pages} pages failed to fetch; aborting"
                    ) from exc
                if known_total_pages is None and consecutive_failures >= 3 and total_items > 0:
                    # Without a known page count we can't tell "broken page" from "past the
                    # end of the feed": if the final page fails, probing onward would burn the
                    # whole skip budget on phantom pages and discard a nearly-complete
                    # snapshot. Three consecutive failures with no total — after at least one
                    # good page (a dead-from-page-1 outage must still abort loudly, not yield
                    # an empty "successful" snapshot) -> assume end.
                    logger.warning("bondcentral.assumed_end_of_feed", page=page)
                    break
                if (known_total_pages is not None and page >= known_total_pages) or (
                    max_pages is not None and page >= max_pages
                ):
                    break
                page += 1
                continue
            consecutive_failures = 0
            items = payload.get("data") or []
            kept = 0
            for item in items:
                record = _parse_item(item)
                if record is not None:
                    kept += 1
                    yield record
            total_bytes += page_bytes
            total_items += len(items)
            total_kept += kept
            info = payload.get("pagination_info") or {}
            known_total_pages = info.get("total_pages") or known_total_pages
            logger.info(
                "bondcentral.page",
                page=page,
                total_pages=info.get("total_pages"),
                items=len(items),
                kept=kept,
                dropped=len(items) - kept,
            )
            if not info.get("has_next"):
                break
            if max_pages is not None and page >= max_pages:
                break
            page += 1
        if skipped_pages:
            logger.warning("bondcentral.pages_skipped_total", skipped=skipped_pages)
        self.add_metric(
            "universe",
            bytes_downloaded=total_bytes,
            rows_extracted=total_items,
            rows_parsed=total_kept,
            rows_dropped=total_items - total_kept,
        )

    def fetch_reference(self, isin: str) -> SecurityRecord | None:
        """Fetch one security's reference data by ISIN (for enrichment); ``None`` if not covered.

        The detail lookup ``/securities/?isin=X`` returns the same ``{isin, data:{...}}`` shape as
        the list, so it reuses :func:`_parse_item` (coupon_rate, maturity_date, issuer, etc.).
        """
        response = self._client.get(
            _URL,
            params={"isin": isin, "page": "1", "size": "1"},
            headers={"Accept": "application/json", "Origin": _ORIGIN},
        )
        try:
            payload = response.json()
        except ValueError as exc:  # challenge/error page mid-enrichment: fail this ISIN cleanly
            raise SourceError(f"BondCentral reference for {isin}: non-JSON response") from exc
        items = (payload.get("data") or []) if isinstance(payload, dict) else []
        record = _parse_item(items[0]) if items else None
        if record is not None and record.isin != isin:
            # If the API ever ignores the isin filter, enrichment must not write another
            # security's coupon/maturity onto the requested row.
            logger.warning("bondcentral.reference_mismatch", requested=isin, received=record.isin)
            return None
        return record


# ---------------------------------------------------------------------- parsing
def _parse_item(item: dict[str, Any]) -> SecurityRecord | None:
    """Parse one ``{"isin", "data": {...}}`` item into a record, or ``None`` if invalid."""
    data = item.get("data") or {}
    isin = (item.get("isin") or data.get("isin") or "").strip()
    if len(isin) != 12 or not isin.startswith("IN"):
        return None
    rating, agency, rating_date = _first_rating(data.get("ratings"))
    return SecurityRecord(
        isin=isin,
        instrument_type=InstrumentType.CORP,
        source=BondCentralSource.name,
        description=_as_str(data.get("security_name")),
        issuer=_as_str(data.get("issuer")),
        coupon=_as_float(data.get("coupon_rate")),
        interest_type=_as_str(data.get("interest_type")),
        maturity_date=_as_date(data.get("maturity_date")),
        face_value=_as_float(data.get("face_value")),
        attributes={
            "credit_rating": rating,
            "credit_rating_agency": agency,
            "credit_rating_date": rating_date,
            "security_status": _as_str(data.get("security_status")),
            "secured_unsecured": _as_str(data.get("secured_unsecured")),
        },
    )


def _first_rating(ratings: Any) -> tuple[str | None, str | None, str | None]:
    """Return ``(rating, agency, date)`` from the first entry with a non-null ``cra_rating``."""
    if isinstance(ratings, list):
        for entry in ratings:
            if isinstance(entry, dict):
                value = _as_str(entry.get("cra_rating"))
                if value:
                    return (
                        value,
                        _as_str(entry.get("credit_rating_agency_name")),
                        _as_str(entry.get("date_of_credit_rating")),
                    )
    return None, None, None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, str) and value.strip():
        try:
            return dt.datetime.fromisoformat(value.strip()).replace(tzinfo=dt.UTC).date()
        except ValueError:
            for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
                try:
                    return dt.datetime.strptime(value.strip(), fmt).replace(tzinfo=dt.UTC).date()
                except ValueError:
                    continue
    return None
