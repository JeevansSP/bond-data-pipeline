"""CCIL connector (planned) — G-Sec / NDS-OM secondary trades & settlement.

Liferay portlet resource calls (Akamai-gated, market-hours only), e.g. individual-trades,
reported-deals, market-by-price, outright-and-repo-settlement.
See docs/research/2026-07-18_113141_ccilindia.com.md.
"""

from __future__ import annotations

import datetime as dt
from typing import Final


class CcilSource:
    """Fetches G-Sec NDS-OM trades/settlement (intraday; sovereign secondary market)."""

    name: Final = "ccil"

    def fetch_individual_trades(
        self, sec_type: str, as_of: dt.date
    ) -> list[object]:  # pragma: no cover
        """Return NDS-OM individual trades for ``sec_type`` (Central Govt / State Govt / Tbills)."""
        raise NotImplementedError("CCIL trade ingestion not yet implemented (portlet + Akamai)")
