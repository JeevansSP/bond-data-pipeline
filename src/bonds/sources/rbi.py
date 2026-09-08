"""RBI connector — sovereign auction announcements (calendar level).

Index (browser-like Accept headers): ``https://www.rbi.org.in/scripts/FS_PressRelease.aspx?fn=2757``
lists auction press releases; each has an HTML detail page (``?prid=NNNNN``) carrying the auction
date and a results table, plus a PDF. See ``docs/research/2026-07-18_120554_rbi.org.in.md``.

v1 captures the calendar: title, auction type, date (from the detail page), and detail/PDF links.
Per-auction financials (cut-off yield, notified/accepted amounts) live in the detail page's
transposed results table (securities as columns; layout varies by auction type) — a documented
follow-up, not parsed here.

NOTE on ``auction_date`` semantics: the date parsed from the detail page is the press-release
*publication* date. For result-type releases that coincides with the auction; for announcement
releases it precedes the auction by days. Parsing the true auction date from the release body
(layout varies per auction type) is the same documented follow-up as the financials.
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
from bonds.models import RbiAuctionRecord, RbiAuctionResultRecord
from bonds.quality.metrics import MetricsCollector
from bonds.sources.base import SourceError
from bonds.sources.rbi_auction_result import parse_auction_results

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


class RbiSource(MetricsCollector):
    """Fetches and parses the RBI sovereign auction calendar."""

    name: Final = "rbi"

    def __init__(
        self, client: ThrottledClient | None = None, settings: Settings | None = None
    ) -> None:
        self.reset_metrics()
        self._settings = settings or get_settings()
        self._client = client or ThrottledClient(self._settings.http)
        # fetch_auctions downloads every detail page to read its date, and fetch_auction_results
        # wants the same bodies. Cached per instance so one nightly pass costs one fetch per
        # release rather than two; share the instance to make it one for the whole run.
        self._detail_cache: dict[str, bytes] = {}

    def _raw_path(self, as_of: dt.date) -> Path:
        return self._settings.data_dir / "raw" / self.name / f"auctions_{as_of.isoformat()}.html"

    def fetch_auctions(self, as_of: dt.date) -> list[RbiAuctionRecord]:
        """Parse the auction index, then enrich each with its press-release date.

        See the module note on ``auction_date`` semantics.
        """
        self.reset_metrics()
        response = self._client.get(_INDEX_URL, headers=_HEADERS)
        content = response.content
        path = self._raw_path(as_of)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("rbi.index_downloaded", bytes=len(content))

        records = parse_index(content, source=self.name)
        enriched = [r.model_copy(update={"auction_date": self._detail_date(r)}) for r in records]
        dated = sum(1 for r in enriched if r.auction_date is not None)
        self.add_metric(
            "auction_index",
            bytes_downloaded=len(content),
            rows_extracted=len(records),
            rows_parsed=len(enriched),
        )
        logger.info("rbi.parsed", auctions=len(enriched), with_date=dated)
        return enriched

    def fetch_auction_results(
        self, auctions: list[RbiAuctionRecord]
    ) -> list[RbiAuctionResultRecord]:
        """Fetch and parse the "Full Auction Result" release behind each result announcement.

        Only the *full* result releases carry per-security figures; the companion "Cut-off"
        releases are a one-line summary and the announcements are a calendar. Filtering on the
        title (see :func:`is_full_result`) avoids a request per calendar entry.

        A single unreachable release is logged and skipped rather than aborting the batch — one
        flaky press-release page must not cost the whole day's results.
        """
        results: list[RbiAuctionResultRecord] = []
        for auction in auctions:
            if not auction.detail_url or not is_full_result(auction.title):
                continue
            content = self._detail_body(auction.detail_url, prid=auction.prid)
            if content is None:
                continue
            path = self._settings.data_dir / "raw" / self.name / "results" / f"{auction.prid}.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            parsed = parse_auction_results(
                content,
                prid=auction.prid,
                source=self.name,
                auction_date=auction.auction_date,
                auction_type=auction.auction_type,
            )
            self.add_metric(
                f"result/{auction.prid}",
                bytes_downloaded=len(content),
                rows_extracted=len(parsed),
                rows_parsed=len(parsed),
            )
            results.extend(parsed)
        logger.info("rbi.results_parsed", securities=len(results))
        return results

    def _detail_body(self, url: str, *, prid: str) -> bytes | None:
        """Fetch a press-release body, reusing anything already downloaded this run.

        A single flaky/404 page must not abort the whole ingest, so a failure is logged and
        returns ``None`` rather than raising.
        """
        if url in self._detail_cache:
            return self._detail_cache[url]
        try:
            response = self._client.get(url, headers=_HEADERS)
        except httpx.HTTPError:
            logger.warning("rbi.detail_fetch_failed", prid=prid)
            return None
        self._detail_cache[url] = response.content
        return response.content

    def _detail_date(self, record: RbiAuctionRecord) -> dt.date | None:
        if not record.detail_url:
            return None
        content = self._detail_body(record.detail_url, prid=record.prid)
        return parse_detail_date(content) if content is not None else None


def is_full_result(title: str) -> bool:
    """Whether a press-release title is a *full* auction result (per-security figures).

    RBI publishes two releases per auction: a "Cut-off" summary and a "Full Auction Result" with
    the per-security table. Only the latter is worth fetching. Underwriting and buyback results
    are excluded — they are auctions of a different thing (commission bids, repurchases) and do
    not carry the notified/cut-off shape this parser reads.
    """
    lowered = title.lower()
    if "underwriting" in lowered or "buyback" in lowered:
        return False
    return "full auction result" in lowered or (
        "auction result" in lowered and "cut-off" not in lowered and "cut off" not in lowered
    )


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
    """Extract the ``Date : Mon DD, YYYY`` press-release date from an auction detail page.

    This is the *publication* date, not necessarily the auction date — see the module note.
    """
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
