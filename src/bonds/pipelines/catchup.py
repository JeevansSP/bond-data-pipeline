"""Idempotent daily catch-up for unattended scheduling (launchd/systemd).

A daily scheduler may miss runs when the machine is asleep or offline. This module makes a single
invocation self-healing:

* **Date-series sources** (FBIL sovereign valuations + yield curves, CCIL trades, BSE/NSE
  trade-level corporate trades, NSE CBM daily archive, NSE bond report) are gap-filled for
  *every* missed business day — from the day after the source's last processed date up to
  ``as_of`` — bounded to ``max_gap_days`` so a fresh or long-idle database never triggers a
  runaway backfill.
* **Snapshot / latest-session sources** (universe, SEBI public issues, RBI auctions, NSE trades)
  represent current state and are simply refreshed once for ``as_of``.

Every write is idempotent (``ON CONFLICT`` upserts keyed by ``(source, dataset, run_date)``), so
re-running — whether twice in a day or after an outage — converges rather than duplicating.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

from bonds.calendar import business_days
from bonds.logging import get_logger
from bonds.pipelines.base import PipelineResult
from bonds.pipelines.bond_report import NseBondReportPipeline
from bonds.pipelines.corporate_trade import (
    BseCorporateTradePipeline,
    NseCorporateTradePipeline,
)
from bonds.pipelines.public_issue import PublicIssuePipeline
from bonds.pipelines.rbi_auction import RbiAuctionPipeline
from bonds.pipelines.sovereign_valuation import DEFAULT_PRODUCTS, SovereignValuationPipeline
from bonds.pipelines.trade import TradePipeline
from bonds.pipelines.universe import UniversePipeline
from bonds.pipelines.yield_curve import DEFAULT_CURVE_PRODUCTS, YieldCurvePipeline
from bonds.sources.bse import BseSource
from bonds.sources.ccil_historical import CcilHistoricalTradesSource, derive_securities
from bonds.sources.fbil import FbilSource
from bonds.sources.nse import NseSource
from bonds.sources.nse import derive_securities as derive_nse_securities
from bonds.sources.nse_bond_report import NseBondReportSource
from bonds.sources.nse_cbm import NseCbmDailySource
from bonds.sources.nse_trade_settlement import NseTradeSettlementSource
from bonds.storage import Database
from bonds.storage.repositories import DatasetProgress, IngestionRunRepository

logger = get_logger(__name__)

# Dataset ids captured at import time: tests monkeypatch the pipeline classes with fakes
# that don't carry the class attribute.
_BSE_CORP_TRADES_DATASET = BseCorporateTradePipeline.dataset
_NSE_CORP_TRADES_DATASET = NseCorporateTradePipeline.dataset

DEFAULT_MAX_GAP_DAYS = 30
# A skip may mean "holiday" or "ran before the source published" (FBIL 500s for both). Re-attempt
# skips this recent; older skips are terminal. Re-fetching a true holiday a few times is one cheap
# request per attempt, while a premature same-day skip is healed by the evening's scheduled run.
DEFAULT_SKIP_RETRY_DAYS = 3


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


def dataset_start(progress: DatasetProgress, *, as_of: dt.date, max_gap_days: int) -> dt.date:
    """First day to (re)ingest for one dataset.

    Resume after the last processed day, but pull back to the oldest still-failed day and the
    oldest recently-skipped day so those are re-attempted (later, already-successful days re-run
    too — upserts make that idempotent). Everything is floored at ``as_of - max_gap_days``.

    A retry candidate *below* the floor is unreachable and must be ignored entirely: pulling the
    start to the floor for it would re-ingest a rolling ``max_gap_days`` window every night,
    forever, without ever re-running (and thus never healing) the failed day itself. Those days
    are surfaced by the ``catchup.failed_days_below_cap`` warning instead.
    """
    floor = as_of - dt.timedelta(days=max_gap_days)
    start = bounded_start(progress.processed_through, as_of=as_of, max_gap_days=max_gap_days)
    for retry in (progress.earliest_failed, progress.earliest_recent_skip):
        if retry is not None and floor <= retry < start:
            start = retry
    return start


def series_start(
    database: Database,
    source: str,
    *,
    as_of: dt.date,
    max_gap_days: int,
    skip_retry_days: int = DEFAULT_SKIP_RETRY_DAYS,
    expected_datasets: Iterable[str] = (),
) -> dt.date:
    """First business day to (re)ingest for a date-series ``source``.

    Computed per dataset (min across the source's datasets), so one product failing on a day
    never lets a sibling product's success advance the anchor past it. See :func:`dataset_start`
    for the per-dataset policy.

    ``expected_datasets`` names the datasets the pipeline *should* be writing: one with no
    history at all (e.g. a newly added FBIL product) gets the full backfill window rather than
    being silently invisible to the min() over historical datasets. When given, the anchor is
    also computed over *only* those datasets — several pipelines can share one source name
    (FBIL valuations vs FBIL curves, NSE session summaries vs NSE trade-level windows) without
    one pipeline's laggard dragging the other's start back.
    """
    skip_retry_cutoff = as_of - dt.timedelta(days=skip_retry_days)
    with database.session() as session:
        progress = IngestionRunRepository(session).dataset_progress(
            source, skip_retry_cutoff=skip_retry_cutoff
        )
    expected = tuple(expected_datasets)
    if expected:
        empty = DatasetProgress(
            processed_through=None, earliest_failed=None, earliest_recent_skip=None
        )
        progress = {dataset: progress.get(dataset, empty) for dataset in expected}
    if not progress:
        return bounded_start(None, as_of=as_of, max_gap_days=max_gap_days)

    floor = as_of - dt.timedelta(days=max_gap_days)
    starts: dict[str, dt.date] = {}
    for dataset, p in progress.items():
        start = dataset_start(p, as_of=as_of, max_gap_days=max_gap_days)
        starts[dataset] = start
        if p.processed_through is not None and start > p.processed_through + dt.timedelta(days=1):
            # The gap exceeds max_gap_days; days between the anchor and the floor won't be caught
            # up and will be recorded as processed, so surface it — the operator must run an
            # explicit backfill for the lost window or those days are gone.
            logger.warning(
                "catchup.gap_exceeds_cap",
                source=source,
                dataset=dataset,
                last_processed=p.processed_through.isoformat(),
                resume_from=start.isoformat(),
                skipped_days=(start - p.processed_through).days - 1,
            )
        if p.earliest_failed is not None and p.earliest_failed < floor:
            # Failed days older than the cap are never retried automatically.
            logger.warning(
                "catchup.failed_days_below_cap",
                source=source,
                dataset=dataset,
                earliest_failed=p.earliest_failed.isoformat(),
                floor=floor.isoformat(),
            )
    return min(starts.values())


def catch_up(
    database: Database, *, as_of: dt.date, max_gap_days: int = DEFAULT_MAX_GAP_DAYS
) -> CatchUpReport:
    """Gap-fill date-series sources through ``as_of`` and refresh snapshot sources for ``as_of``."""
    groups: dict[str, list[PipelineResult]] = {}

    # --- date-series: gap-fill every missed business day -------------------------------------
    fbil_start = series_start(
        database,
        FbilSource.name,
        as_of=as_of,
        max_gap_days=max_gap_days,
        expected_datasets=[f"{FbilSource.name}.{p}" for p in DEFAULT_PRODUCTS],
    )
    fbil_days = list(business_days(fbil_start, as_of)) if fbil_start <= as_of else []
    logger.info("catchup.fbil", start=fbil_start.isoformat(), n=len(fbil_days))
    groups["Sovereign valuations · FBIL"] = (
        SovereignValuationPipeline(database).backfill(fbil_start, as_of) if fbil_days else []
    )

    curve_start = series_start(
        database,
        FbilSource.name,
        as_of=as_of,
        max_gap_days=max_gap_days,
        expected_datasets=[f"{FbilSource.name}.curve.{p}" for p in DEFAULT_CURVE_PRODUCTS],
    )
    curve_days = list(business_days(curve_start, as_of)) if curve_start <= as_of else []
    logger.info("catchup.fbil_curves", start=curve_start.isoformat(), n=len(curve_days))
    groups["Yield curves · FBIL"] = (
        YieldCurvePipeline(database).backfill(curve_start, as_of) if curve_days else []
    )

    ccil_start = series_start(
        database,
        CcilHistoricalTradesSource.name,
        as_of=as_of,
        max_gap_days=max_gap_days,
        expected_datasets=[f"{CcilHistoricalTradesSource.name}.trades"],
    )
    ccil_pipeline = TradePipeline(
        database, source=CcilHistoricalTradesSource(), derive_securities=derive_securities
    )
    ccil_days = list(business_days(ccil_start, as_of)) if ccil_start <= as_of else []
    logger.info("catchup.ccil", start=ccil_start.isoformat(), n=len(ccil_days))
    groups["G-Sec/T-Bill trades · CCIL"] = [ccil_pipeline.run(day) for day in ccil_days]

    bse_start = series_start(
        database,
        BseSource.name,
        as_of=as_of,
        max_gap_days=max_gap_days,
        expected_datasets=[_BSE_CORP_TRADES_DATASET],
    )
    bse_days = list(business_days(bse_start, as_of)) if bse_start <= as_of else []
    logger.info("catchup.bse_corp_trades", start=bse_start.isoformat(), n=len(bse_days))
    groups["Corp trades (trade-level) · BSE"] = (
        BseCorporateTradePipeline(database).backfill(bse_start, as_of) if bse_days else []
    )

    nse_ts_start = series_start(
        database,
        NseTradeSettlementSource.name,
        as_of=as_of,
        max_gap_days=max_gap_days,
        expected_datasets=[_NSE_CORP_TRADES_DATASET],
    )
    logger.info("catchup.nse_corp_trades_ts", start=nse_ts_start.isoformat())
    groups["Corp trades (trade-level) · NSE"] = (
        NseCorporateTradePipeline(database).backfill(nse_ts_start, as_of)
        if nse_ts_start <= as_of
        else []
    )

    cbm_start = series_start(
        database,
        NseCbmDailySource.name,
        as_of=as_of,
        max_gap_days=max_gap_days,
        expected_datasets=[f"{NseCbmDailySource.name}.trades"],
    )
    cbm_pipeline = TradePipeline(database, source=NseCbmDailySource())
    cbm_days = list(business_days(cbm_start, as_of)) if cbm_start <= as_of else []
    logger.info("catchup.nse_cbm", start=cbm_start.isoformat(), n=len(cbm_days))
    groups["Corp trades (daily archive) · NSE"] = [cbm_pipeline.run(day) for day in cbm_days]

    report_start = series_start(
        database,
        NseBondReportSource.name,
        as_of=as_of,
        max_gap_days=max_gap_days,
        expected_datasets=[f"{NseBondReportSource.name}.bond_report"],
    )
    report_days = list(business_days(report_start, as_of)) if report_start <= as_of else []
    logger.info("catchup.nse_bond_report", start=report_start.isoformat(), n=len(report_days))
    groups["Bond master · NSE report"] = (
        NseBondReportPipeline(database).backfill(report_start, as_of) if report_days else []
    )

    # --- snapshot / latest-session: refresh once for as_of ----------------------------------
    groups["Universe · BondCentral"] = [UniversePipeline(database).run(as_of)]
    groups["Public issues · SEBI"] = [PublicIssuePipeline(database).run(as_of)]
    groups["Auctions · RBI"] = [RbiAuctionPipeline(database).run(as_of)]
    groups["Corp trades · NSE"] = [
        TradePipeline(database, source=NseSource(), derive_securities=derive_nse_securities).run(
            as_of
        )
    ]

    return CatchUpReport(as_of=as_of, groups=groups)
