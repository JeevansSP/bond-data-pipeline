"""Idempotent daily catch-up for unattended scheduling (launchd/systemd).

A daily scheduler may miss runs when the machine is asleep or offline. This module makes a single
invocation self-healing:

* **Date-series sources** (FBIL sovereign valuations, CCIL trades) are gap-filled for *every*
  missed business day — from the day after the source's last processed date up to ``as_of`` —
  bounded to ``max_gap_days`` so a fresh or long-idle database never triggers a runaway backfill.
* **Snapshot / latest-session sources** (universe, SEBI public issues, RBI auctions, NSE trades)
  represent current state and are simply refreshed once for ``as_of``.

Every write is idempotent (``ON CONFLICT`` upserts keyed by ``(source, dataset, run_date)``), so
re-running — whether twice in a day or after an outage — converges rather than duplicating.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from bonds.calendar import business_days
from bonds.logging import get_logger
from bonds.pipelines.base import PipelineResult
from bonds.pipelines.public_issue import PublicIssuePipeline
from bonds.pipelines.rbi_auction import RbiAuctionPipeline
from bonds.pipelines.sovereign_valuation import SovereignValuationPipeline
from bonds.pipelines.trade import TradePipeline
from bonds.pipelines.universe import UniversePipeline
from bonds.sources.ccil_historical import CcilHistoricalTradesSource
from bonds.sources.nse import NseSource
from bonds.storage import Database
from bonds.storage.repositories import IngestionRunRepository

logger = get_logger(__name__)

DEFAULT_MAX_GAP_DAYS = 30


@dataclass(frozen=True, slots=True)
class CatchUpReport:
    """Per-group results of a catch-up run (``group label -> pipeline results``)."""

    as_of: dt.date
    groups: dict[str, list[PipelineResult]] = field(default_factory=dict)

    @property
    def results(self) -> list[PipelineResult]:
        """All pipeline results, flattened."""
        return [r for group in self.groups.values() for r in group]


def bounded_start(anchor: dt.date | None, *, as_of: dt.date, max_gap_days: int) -> dt.date:
    """First day to (re)ingest given a source's last processed date.

    The day after ``anchor``, floored at ``as_of - max_gap_days`` so an empty history (``anchor``
    is ``None``) or a long gap backfills a bounded window rather than years of data.
    """
    floor = as_of - dt.timedelta(days=max_gap_days)
    if anchor is None:
        return floor
    return max(anchor + dt.timedelta(days=1), floor)


def series_start(database: Database, source: str, *, as_of: dt.date, max_gap_days: int) -> dt.date:
    """First business day to (re)ingest for a date-series ``source`` (see :func:`bounded_start`)."""
    with database.session() as session:
        anchor = IngestionRunRepository(session).last_processed_date(source)
    return bounded_start(anchor, as_of=as_of, max_gap_days=max_gap_days)


def catch_up(
    database: Database, *, as_of: dt.date, max_gap_days: int = DEFAULT_MAX_GAP_DAYS
) -> CatchUpReport:
    """Gap-fill date-series sources through ``as_of`` and refresh snapshot sources for ``as_of``."""
    groups: dict[str, list[PipelineResult]] = {}

    # --- date-series: gap-fill every missed business day -------------------------------------
    fbil_start = series_start(database, "fbil", as_of=as_of, max_gap_days=max_gap_days)
    fbil_days = list(business_days(fbil_start, as_of)) if fbil_start <= as_of else []
    logger.info("catchup.fbil", start=fbil_start.isoformat(), n=len(fbil_days))
    groups["Sovereign valuations · FBIL"] = (
        SovereignValuationPipeline(database).backfill(fbil_start, as_of) if fbil_days else []
    )

    ccil_start = series_start(database, "ccil", as_of=as_of, max_gap_days=max_gap_days)
    ccil_pipeline = TradePipeline(database, source=CcilHistoricalTradesSource())
    ccil_days = list(business_days(ccil_start, as_of)) if ccil_start <= as_of else []
    logger.info("catchup.ccil", start=ccil_start.isoformat(), n=len(ccil_days))
    groups["G-Sec/T-Bill trades · CCIL"] = [ccil_pipeline.run(day) for day in ccil_days]

    # --- snapshot / latest-session: refresh once for as_of ----------------------------------
    groups["Universe · BondCentral"] = [UniversePipeline(database).run(as_of)]
    groups["Public issues · SEBI"] = [PublicIssuePipeline(database).run(as_of)]
    groups["Auctions · RBI"] = [RbiAuctionPipeline(database).run(as_of)]
    groups["Corp trades · NSE"] = [TradePipeline(database, source=NseSource()).run(as_of)]

    return CatchUpReport(as_of=as_of, groups=groups)
