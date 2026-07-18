"""CDSL connector (planned) — corporate issued/outstanding half-yearly snapshots.

Endpoint (no auth): ``https://www.cdslindia.com/CorporateBond/IssuerReportDetails.aspx?ReportDate=DDMMYYYY``
(HTML table: Issuer, ISIN, issuance/maturity, coupon, amount issued & outstanding; back to 2017).
See docs/research/2026-07-18_113141_cdslindia.com.md.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from bonds.models import SecurityRecord


class CdslSource:
    """Fetches corporate issued/outstanding snapshots (pillar 1 / attribute history)."""

    name: Final = "cdsl"

    def fetch_snapshot(self, report_date: dt.date) -> list[SecurityRecord]:  # pragma: no cover
        """Return the issuer/outstanding snapshot for a half-yearly ``report_date``."""
        raise NotImplementedError("CDSL snapshot ingestion not yet implemented")
