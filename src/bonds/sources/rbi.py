"""RBI connector — sovereign auction announcements (calendar level).

Index (browser-like Accept headers): ``https://www.rbi.org.in/scripts/FS_PressRelease.aspx?fn=2757``
lists auction press releases; each has an HTML detail page (``?prid=NNNNN``) carrying the auction
date and a results table, plus a PDF. See ``docs/research/2026-07-18_120554_rbi.org.in.md``.

v1 captures the calendar: title, auction type, date (from the detail page), and detail/PDF links.
Per-auction financials (cut-off yield, notified/accepted amounts) live in the detail page's
transposed results table (securities as columns; layout varies by auction type) — a documented
follow-up, not parsed here.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Final, cast
from urllib.parse import urljoin

import httpx
from lxml.html import HtmlElement, fromstring

from bonds.config import Settings, get_settings
from bonds.http import ThrottledClient
from bonds.logging import get_logger
from bonds.models import RbiAuctionRecord
from bonds.sources.base import SourceError

logger = get_logger(__name__)

_BASE: Final = "https://www.rbi.org.in/scripts/"
_INDEX_URL: Final = "https://www.rbi.org.in/scripts/FS_PressRelease.aspx?fn=2757"
_HEADERS: Final = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_PRID_RE: Final = re.compile(r"prid=(\d+)")
_DATE_RE: Final = re.compile(r"Date\s*:\s*([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})")

# Title keyword -> auction type classification (checked in order).
_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("treasury bill", "T-Bill"),
    ("state government", "SDL"),
    ("government stock", "G-Sec"),
    ("dated securit", "G-Sec"),
    ("underwriting", "Underwriting"),
    ("sovereign gold", "SGB"),
)
_AUCTION_KEYWORDS: Final = ("auction", "treasury bill", "government stock", "state government")


class RbiSource:
    """Fetches and parses the RBI sovereign auction calendar."""

    name: Final = "rbi"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)

    def _raw_path(self, as_of: dt.date) -> Path:
        return self._settings.data_dir / "raw" / self.name / f"auctions_{as_of.isoformat()}.html"

    def fetch_auctions(self, as_of: dt.date) -> list[RbiAuctionRecord]:
        """Parse the auction index, then enrich each with its date from the detail page."""
        response = self._client.get(_INDEX_URL, headers=_HEADERS)
        content = response.content
        path = self._raw_path(as_of)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("rbi.index_downloaded", bytes=len(content))

        records = parse_index(content, source=self.name)
        enriched = [r.model_copy(update={"auction_date": self._detail_date(r)}) for r in records]
        dated = sum(1 for r in enriched if r.auction_date is not None)
        logger.info("rbi.parsed", auctions=len(enriched), with_date=dated)
        return enriched

    def _detail_date(self, record: RbiAuctionRecord) -> dt.date | None:
        if not record.detail_url:
            return None
        try:
            detail = self._client.get(record.detail_url, headers=_HEADERS)
        except (httpx.HTTPError, SourceError):
            # A single flaky/404 detail page must not abort the whole auction ingest.
            logger.warning("rbi.detail_fetch_failed", prid=record.prid)
            return None
        return parse_detail_date(detail.content)


def parse_index(content: bytes, *, source: str) -> list[RbiAuctionRecord]:
    """Parse the auction press-release index into calendar records (no dates yet)."""
    root = fromstring(content)
    by_prid: dict[str, RbiAuctionRecord] = {}
    for anchor in cast("list[HtmlElement]", root.xpath("//a[contains(@href,'prid=')]")):
        title = " ".join(anchor.text_content().split())
        href = anchor.get("href") or ""
        match = _PRID_RE.search(href)
        if not match or not _is_auction(title):
            continue
        prid = match.group(1)
        by_prid[prid] = RbiAuctionRecord(
            prid=prid,
            title=title,
            auction_type=_classify(title),
            source=source,
            detail_url=urljoin(_BASE, href),
            pdf_url=_row_pdf(anchor),
        )
    if not by_prid:
        raise SourceError("no auction rows parsed from RBI index (layout changed?)")
    return list(by_prid.values())


def parse_detail_date(content: bytes) -> dt.date | None:
    """Extract the ``Date : Mon DD, YYYY`` from an auction detail page."""
    root = cast("HtmlElement", fromstring(content))
    text = " ".join(root.text_content().split())
    match = _DATE_RE.search(text)
    if not match:
        return None
    month, day, year = match.groups()
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return dt.datetime.strptime(f"{month} {day} {year}", fmt).replace(tzinfo=dt.UTC).date()
        except ValueError:
            continue
    return None


def _is_auction(title: str) -> bool:
    lowered = title.lower()
    return any(kw in lowered for kw in _AUCTION_KEYWORDS)


def _classify(title: str) -> str:
    lowered = title.lower()
    for keyword, label in _TYPES:
        if keyword in lowered:
            return label
    return "Other"


def _row_pdf(anchor: HtmlElement) -> str | None:
    node: HtmlElement | None = anchor
    for _ in range(4):
        node = node.getparent() if node is not None else None
        if node is None:
            break
        if node.tag == "tr":
            pdfs = cast(
                "list[HtmlElement]",
                node.xpath(".//a[contains(translate(@href,'PDF','pdf'),'.pdf')]"),
            )
            return pdfs[0].get("href") if pdfs else None
    return None
