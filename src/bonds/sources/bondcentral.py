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
# BondCentral persistently 500s on several ~100-record windows (~6 of 256 pages observed, ~2.4%,
# the same pages every day). Almost every record in them is fine: each broken window holds ONE
# record the API cannot serialise, and any page containing it 500s whatever the page size
# (verified 2026-09-19 — page 253 @100 -> 500; pages 2521-2530 @10 -> the same 100 ISINs in the
# same order except 2523; records 25221-25230 @1 -> all 200 except 25223). So a failed page is
# re-read down a ladder of sizes, 100 -> 10 -> 1, and only the unserialisable records are lost:
# ~7 a day instead of ~600. The skip cap still trips a genuine outage (>~12% of pages failing):
# once the previous page has failed as well, sub-paging gives up on a page as soon as its first
# sub-page fails, so an outage costs one extra request per page, not ten. (A page whose
# predecessor succeeded gets all its sub-pages tried — the feed was demonstrably up one request
# ago, so a failing first slice is a broken slice, not an outage; the first live run lost 90 good
# records of page 200 to bailing on its first slice.)
_MAX_SKIPPED_PAGES: Final = 30
_LADDER_STEP: Final = 10  # a failed page of n records is re-read as 10 pages of n/10


class BondCentralSource(MetricsCollector):
    """Paginates the BondCentral securities master into :class:`SecurityRecord` objects."""

    name: Final = "bondcentral"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    def _raw_path(self, as_of: dt.date, page: int, size: int) -> Path:
        # Full-size pages keep their historical name; a sub-page carries its size so a recovered
        # window's files sit next to — and cannot collide with — the ordinary page files.
        stem = f"page_{page:04d}" if size == _MAX_PAGE_SIZE else f"page_{page:04d}_size{size:03d}"
        return self._settings.data_dir / "raw" / self.name / as_of.isoformat() / f"{stem}.json"

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
        path = self._raw_path(as_of, page, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload, len(response.content)

    def _page_items(
        self, page: int, size: int, as_of: dt.date, *, feed_alive: bool
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any], int]:
        """``(items, bytes, pagination_info, records lost)`` for one page.

        A page that fails at full size is re-read down the size ladder (:meth:`_recover_page`).
        The original error propagates when the page cannot be split, when nothing in it could be
        recovered, or — unless ``feed_alive`` (the previous page succeeded) — when its first
        sub-page fails.
        """
        try:
            payload, page_bytes = self._fetch_page(page, size, as_of)
        except (httpx.HTTPError, ValueError) as exc:
            if not _splittable(size):
                raise
            logger.warning("bondcentral.page_failed", page=page, error=str(exc)[:80])
            return self._recover_page(page, size, as_of, cause=exc, bail_on_first=not feed_alive)
        items: list[dict[str, Any]] = list(payload.get("data") or [])
        return items, page_bytes, dict(payload.get("pagination_info") or {}), 0

    def _recover_page(
        self, page: int, size: int, as_of: dt.date, *, cause: Exception, bail_on_first: bool
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any], int]:
        """Re-read a failed ``size``-record page as ten pages of ``size / 10``, recursively.

        The API pages a stable ordering, so sub-page ``(page - 1) * 10 + i`` at size ``size / 10``
        is exactly the ``i``-th slice of ``page``. A slice that fails is itself re-read one rung
        down, until single records: the one unserialisable record costs one record, not its
        whole window. ``has_next`` is taken from the last slice (a middle slice's ``has_next``
        only says the next slice exists); if that slice is lost it falls back to the record
        count, so a lost tail can never end the crawl early.

        Raises ``cause`` (the full-page error) when nothing was recovered, or — with
        ``bail_on_first`` — as soon as the first slice fails: with the previous page already
        failed that is the feed being down, not a broken window, and the caller's skip budget
        must see it rather than burn ten requests per page.
        """
        sub_size = size // _LADDER_STEP
        first = (page - 1) * _LADDER_STEP + 1
        last = first + _LADDER_STEP - 1
        items: list[dict[str, Any]] = []
        total_bytes = lost = 0
        has_next: bool | None = None
        total_records: int | None = None
        for sub in range(first, last + 1):
            try:
                payload, sub_bytes = self._fetch_page(sub, sub_size, as_of)
                sub_items: list[dict[str, Any]] = list(payload.get("data") or [])
                info: dict[str, Any] = dict(payload.get("pagination_info") or {})
                sub_lost = 0
            except (httpx.HTTPError, ValueError) as exc:
                if sub == first and bail_on_first:
                    raise cause from exc
                try:
                    if not _splittable(sub_size):
                        raise
                    sub_items, sub_bytes, info, sub_lost = self._recover_page(
                        sub, sub_size, as_of, cause=exc, bail_on_first=False
                    )
                except (httpx.HTTPError, ValueError) as sub_exc:
                    logger.warning(
                        "bondcentral.subpage_skipped",
                        page=page,
                        subpage=sub,
                        size=sub_size,
                        error=str(sub_exc)[:80],
                    )
                    sub_items, sub_bytes, info, sub_lost = [], 0, {}, sub_size
            items.extend(sub_items)
            total_bytes += sub_bytes
            lost += sub_lost
            total_records = info.get("total_records") or total_records
            if sub == last and "has_next" in info:
                has_next = info["has_next"]
        if not items:
            raise cause  # nothing recovered: account for it as a failed page, not a recovered one
        total_pages = -(-total_records // size) if total_records else None
        if has_next is None:
            has_next = total_pages is None or page < total_pages
        logger.warning(
            "bondcentral.page_recovered", page=page, size=size, items=len(items), records_lost=lost
        )
        info = {"total_pages": total_pages, "total_records": total_records, "has_next": has_next}
        return items, total_bytes, info, lost

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
        total_bytes = total_items = total_kept = skipped_pages = lost_records = 0
        consecutive_failures = 0
        known_total_pages: int | None = None
        while True:
            try:
                items, page_bytes, info, lost = self._page_items(
                    page, size, as_of, feed_alive=consecutive_failures == 0 and total_items > 0
                )
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
            lost_records += lost
            kept = 0
            for item in items:
                record = _parse_item(item)
                if record is not None:
                    kept += 1
                    yield record
            total_bytes += page_bytes
            total_items += len(items)
            total_kept += kept
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
        if skipped_pages or lost_records:
            logger.warning(
                "bondcentral.pages_skipped_total",
                skipped=skipped_pages,
                records_lost_in_recovered_pages=lost_records,
            )
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


def _splittable(size: int) -> bool:
    """Whether a failed page of ``size`` records can be re-read as ten smaller pages."""
    return size >= _LADDER_STEP and size % _LADDER_STEP == 0


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
