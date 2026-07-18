"""RBI connector (planned) — sovereign primary-issuance auctions.

Auction press releases (PDF, back to 1990) at
``https://www.rbi.org.in/scripts/FS_PressRelease.aspx?fn=2757`` (GoI-dated / T-Bill / SDL / SGB +
cut-off yields); structured macro yield series via DBIE ``https://data.rbi.org.in/``.
See docs/research/2026-07-18_120554_rbi.org.in.md.
"""

from __future__ import annotations

import datetime as dt
from typing import Final


class RbiSource:
    """Fetches sovereign auction results (primary issuance -> new-ISIN entry + cut-off yield)."""

    name: Final = "rbi"

    def fetch_auction_results(self, on: dt.date) -> list[object]:  # pragma: no cover
        """Return parsed auction results for a given date (PDF extraction)."""
        raise NotImplementedError("RBI auction ingestion not yet implemented (PDF parsing)")
