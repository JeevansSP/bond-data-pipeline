"""BondCentral connector (planned) — corporate securities-master universe.

Endpoint (no auth): ``GET https://api.bondcentral.in/securities/?page=&size=`` (size max 100),
~25,501 ISINs. Working filters: isin, issuer, credit_rating, secured_unsecured.
See docs/research/2026-07-18_112508_bondcentral.in.md.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from bonds.models import SecurityRecord


class BondCentralSource:
    """Fetches the corporate securities-master universe (pillar 1)."""

    name: Final = "bondcentral"

    def fetch_universe(self, as_of: dt.date) -> list[SecurityRecord]:  # pragma: no cover
        """Return the full corporate universe as of ``as_of`` (paged over /securities/)."""
        raise NotImplementedError("BondCentral universe ingestion not yet implemented")
