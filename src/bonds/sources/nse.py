"""NSE connector (planned) — exchange corporate-bond trade feed.

Endpoint (Akamai-gated; needs browser-minted cookies + UA + Referer):
``GET https://www.nseindia.com/api/liveCorp-bonds?index=<seg>&marketType=CBM`` where seg is one of
otctrades_listed / otctrades_unlisted / exchtrades_listed / exchtrades_unlisted. Intraday only.
See docs/research/2026-07-18_113141_nseindia.com.md.
"""

from __future__ import annotations

import datetime as dt
from typing import Final


class NseSource:
    """Fetches exchange corporate-bond trades (intraday; sparse — most bonds are illiquid)."""

    name: Final = "nse"

    def fetch_trades(self, segment: str, as_of: dt.date) -> list[object]:  # pragma: no cover
        """Return the current corporate-bond trade rows for ``segment``."""
        raise NotImplementedError("NSE trade ingestion not yet implemented (Akamai cookie flow)")
