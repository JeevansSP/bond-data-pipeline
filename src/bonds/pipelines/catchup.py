"""Idempotent daily catch-up for unattended scheduling (launchd/systemd).

A daily scheduler may miss runs when the machine is asleep or offline. This module makes a single
invocation self-healing:

* **Date-series sources** (FBIL sovereign valuations + yield curves, CCIL trades, BSE/NSE
  trade-level corporate trades, NSE CBM daily archive, NSE bond report) are gap-filled for
  *every* missed business day — from the day after the source's last processed date up to
  ``as_of`` — bounded to ``max_gap_days`` so a fresh or long-idle database never triggers a
  runaway backfill.
* **Snapshot sources** (universe, SEBI public issues, RBI auctions) represent current state and
  are simply refreshed once for ``as_of``. The NSE *live* trade feed is deliberately absent: at
  the 13:00 schedule it shows a session in progress, which is not a session summary and can
  never be completed later from a live endpoint — the finished session arrives through the NSE
  CBM daily archive and the trade-level report above, both of which gap-fill. (From August to
  2026-09-18 the live snapshot was written as the day's summary: 35-106 rows a weekday against
  585-711 in the archive.) ``bonds ingest nse-trades`` remains for a deliberate after-close pull.
* **Derived products** (liquidity metrics, corporate spread matrix) are recomputed for every
  missed business day and for ``as_of``, after every source above has been brought current —
  they are point-in-time views of the tapes, so a missed day is a missing answer, and running
  them first would describe yesterday's data.

Every write is idempotent (``ON CONFLICT`` upserts keyed by ``(source, dataset, run_date)``), so
re-running — whether twice in a day or after an outage — converges rather than duplicating.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import func, select

from bonds.calendar import business_days
from bonds.logging import get_logger
from bonds.pipelines.base import PipelineResult
from bonds.pipelines.bond_report import NseBondReportPipeline
from bonds.pipelines.corporate_trade import (
    BseCorporateTradePipeline,
    NseCorporateTradePipeline,
)
from bonds.pipelines.liquidity import SOURCE as DERIVED_SOURCE
from bonds.pipelines.liquidity import LiquidityPipeline
from bonds.pipelines.public_issue import PublicIssuePipeline
from bonds.pipelines.rbi_auction import RbiAuctionPipeline
from bonds.pipelines.rbi_auction_result import RbiAuctionResultPipeline
from bonds.pipelines.sovereign_valuation import DEFAULT_PRODUCTS, SovereignValuationPipeline
from bonds.pipelines.spread_matrix import SpreadMatrixPipeline
from bonds.pipelines.trade import TradePipeline
from bonds.pipelines.universe import UniversePipeline
from bonds.pipelines.yield_curve import DEFAULT_CURVE_PRODUCTS, YieldCurvePipeline
from bonds.sources.bse import BseSource
from bonds.sources.ccil_historical import CcilHistoricalTradesSource, derive_securities
from bonds.sources.cdsl import CdslSource
from bonds.sources.fbil import FbilSource
from bonds.sources.nse_bond_report import NseBondReportSource
from bonds.sources.nse_cbm import NseCbmDailySource
from bonds.sources.nse_trade_settlement import NseTradeSettlementSource
from bonds.sources.rbi import RbiSource
from bonds.storage import Database
from bonds.storage.repositories import DatasetProgress, IngestionRunRepository
from bonds.storage.schema import IngestionRun

logger = get_logger(__name__)

# Dataset ids captured at import time: tests monkeypatch the pipeline classes with fakes
# that don't carry the class attribute.
_BSE_CORP_TRADES_DATASET = BseCorporateTradePipeline.dataset
_NSE_CORP_TRADES_DATASET = NseCorporateTradePipeline.dataset
_LIQUIDITY_DATASET = LiquidityPipeline.dataset
_SPREAD_MATRIX_DATASET = SpreadMatrixPipeline.dataset

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


def derived_days(
    database: Database, dataset: str, *, as_of: dt.date, max_gap_days: int
) -> list[dt.date]:
    """Business days a derived product must be recomputed for: every missed one, plus ``as_of``.

    The derived tables are point-in-time — each row answers "as at this date" — so a day the
    scheduler missed is a day with no answer, and it is gap-filled like any other series
    (:func:`series_start`, same cap). ``as_of`` is always recomputed, even on a repeat run or a
    Sunday: the tapes it summarises were refreshed moments ago.
    """
    start = series_start(
        database,
        DERIVED_SOURCE,
        as_of=as_of,
        max_gap_days=max_gap_days,
        expected_datasets=[dataset],
    )
    days = list(business_days(start, as_of)) if start <= as_of else []
    if not days or days[-1] != as_of:
        days.append(as_of)
    return days


def latest_half_yearly_snapshot(as_of: dt.date) -> dt.date:
    """The most recent CDSL report date (31 March / 30 September) on or before ``as_of``."""
    year = as_of.year
    for candidate in (dt.date(year, 9, 30), dt.date(year, 3, 31), dt.date(year - 1, 9, 30)):
        if candidate <= as_of:
            return candidate
    raise AssertionError("unreachable: one of the three candidates always precedes as_of")


def cdsl_snapshot_due(database: Database, *, as_of: dt.date) -> dt.date | None:
    """The CDSL report date still to ingest, or ``None`` when the latest one is already in.

    CDSL is a half-yearly snapshot (31-Mar / 30-Sep) so it belongs in no daily loop — but it was
    in no loop at all: a single manual run in July 2026 loaded the 2025-09-30 file and the
    amount-outstanding history then sat 11 months stale. Re-attempting the due snapshot every
    night costs one request until it publishes (CDSL posts days after the report date, and an
    unpublished date records SKIPPED), then nothing for six months.

    The predicate is "has this report date ever loaded **successfully**", not the usual
    ``processed_through``: that anchor is the newest run_date with a success *or a skip*
    (:meth:`IngestionRunRepository.dataset_progress`), so the first attempt at an unpublished
    date would record SKIPPED, advance the anchor to the snapshot date, and stop every later
    night from retrying — missing the file for six months, which is exactly the staleness this
    function exists to prevent.
    """
    snapshot = latest_half_yearly_snapshot(as_of)
    with database.session() as session:
        loaded = session.execute(
            select(func.count())
            .select_from(IngestionRun)
            .where(
                IngestionRun.dataset == f"{CdslSource.name}.universe",
                IngestionRun.run_date == snapshot,
                IngestionRun.status == "success",
            )
        ).scalar_one()
    return None if loaded else snapshot


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

    cdsl_snapshot = cdsl_snapshot_due(database, as_of=as_of)
    logger.info("catchup.cdsl", snapshot=cdsl_snapshot.isoformat() if cdsl_snapshot else None)
    groups["Amount outstanding · CDSL"] = (
        [UniversePipeline(database, source=CdslSource()).run(cdsl_snapshot)]
        if cdsl_snapshot is not None
        else []
    )
    groups["Public issues · SEBI"] = [PublicIssuePipeline(database).run(as_of)]
    # One source instance for both RBI pipelines. Each sweeps the same undated press-release
    # index and then fetches every entry's detail page; sharing the instance (which caches those
    # bodies) turns three passes over rbi.org.in per release into one.
    rbi_source = RbiSource()
    groups["Auctions · RBI"] = [RbiAuctionPipeline(database, source=rbi_source).run(as_of)]
    # The index is not date-scoped, so it always lists the most recent full results: this
    # normally succeeds and re-upserts the same releases rather than skipping. That is the point
    # — a correction reposted by RBI is picked up — and the upsert makes it idempotent.
    groups["Auction results · RBI"] = [
        RbiAuctionResultPipeline(database, source=rbi_source).run(as_of)
    ]

    # --- derived products: recomputed from the tapes above, so they run last ----------------
    # Both are windowed views of the trade tapes rather than ingests, so their gap-fill is a
    # recomputation, not a fetch — but it is still a gap-fill. Every row is keyed to an as-of
    # date and the questions asked of these tables are point-in-time ("was there a print within
    # 15 days of 17 September"), so a missed day is a missing answer: the 2026-09-17/18 outage
    # left no liquidity or spread rows for either date while every tape was healed. Trade dates,
    # not load dates, drive the windows, so recomputing a missed day from the now-complete tapes
    # gives the same as-at view. They run after every source above or the metrics describe
    # yesterday's data.
    liquidity = LiquidityPipeline(database)
    groups["Liquidity metrics · derived"] = [
        liquidity.run(day)
        for day in derived_days(
            database, _LIQUIDITY_DATASET, as_of=as_of, max_gap_days=max_gap_days
        )
    ]
    spread_matrix = SpreadMatrixPipeline(database)
    groups["Spread matrix · derived"] = [
        spread_matrix.run(day)
        for day in derived_days(
            database, _SPREAD_MATRIX_DATASET, as_of=as_of, max_gap_days=max_gap_days
        )
    ]

    return CatchUpReport(as_of=as_of, groups=groups)
