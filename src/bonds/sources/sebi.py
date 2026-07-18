"""SEBI connector (planned) — NCD public-issue calendar.

Static HTML tables (browser-like client; raw curl intermittently 530s):
``https://www.sebi.gov.in/statistics/corporate-bonds/publicissuedata.html`` — company, open/close
dates, base & final issue size, FY2008-09 onward.
See ``docs/research/2026-07-18_113141_sebi.gov.in.md``.
"""

from __future__ import annotations

from typing import Final


class SebiSource:
    """Fetches the NCD public-issue calendar (primary market, public issues only)."""

    name: Final = "sebi"

    def fetch_public_issues(self, financial_year: str) -> list[object]:  # pragma: no cover
        """Return public-issue rows for a financial year (e.g. ``"2025-26"``)."""
        raise NotImplementedError("SEBI public-issue ingestion not yet implemented")
