"""Corporate-trade pipeline: trade-level BSE/NSE RFQ + OTC-reported transactions.

Two feeds, one table (``corporate_trades``):

* BSE Trade & Settlement — one JSON file per day.
* NSE Trade & Settlement — one CSV per (max 7-day) window.

Loads are lake-first (a landed raw file is parsed without re-downloading) and idempotent by
delete-and-replace over the (source, window) being loaded — the feeds publish no transaction
ids, so an upsert key does not exist at this grain.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from sqlalchemy.orm import Session

from bonds.calendar import business_days
from bonds.models import CorporateTradeRecord
from bonds.pipelines.base import PipelineResult, execute_run, persist_file_metrics
from bonds.sources.bse import BseSource
from bonds.sources.nse_trade_settlement import NseTradeSettlementSource
from bonds.storage import Database
from bonds.storage.repositories import CorporateTradeRepository


class DayTradeFetcher(Protocol):
    """A source yielding one day's trade-level records (BSE)."""

    @property
    def name(self) -> str:
        """Stable source identifier."""
        ...

    def fetch_trades(self, date: dt.date) -> list[CorporateTradeRecord]:
        """Fetch + parse one day's trade-level records."""
        ...


class WindowTradeFetcher(Protocol):
    """A source yielding a date-window's trade-level records (NSE)."""

    @property
    def name(self) -> str:
        """Stable source identifier."""
        ...

    def fetch_trades(self, start: dt.date, end: dt.date) -> list[CorporateTradeRecord]:
        """Fetch + parse one window's trade-level records."""
        ...


class BseCorporateTradePipeline:
    """Ingest BSE trade-level corporate trades, one business date at a time."""

    dataset = "bse.corp_trades"

    def __init__(self, database: Database, source: DayTradeFetcher | None = None) -> None:
        self._db = database
        self._source = source or BseSource()

    def run_date(self, date: dt.date) -> PipelineResult:
        """Ingest one business date."""

        def work(session: Session) -> int:
            trades = self._source.fetch_trades(date)  # DataUnavailable on holiday -> SKIPPED
            rows = CorporateTradeRepository(session).replace_window(
                self._source.name, date, date, trades
            )
            persist_file_metrics(
                session,
                self._source,
                source=self._source.name,
                dataset=self.dataset,
                run_date=date,
            )
            return rows

        return execute_run(
            self._db, source=self._source.name, dataset=self.dataset, run_date=date, work=work
        )

    def backfill(self, start: dt.date, end: dt.date) -> list[PipelineResult]:
        """Ingest every business day in ``[start, end]`` (lake-first)."""
        return [self.run_date(day) for day in business_days(start, end)]


class NseCorporateTradePipeline:
    """Ingest NSE trade-level corporate trades in 7-day windows.

    Windows are anchored on 2010-01-04 in fixed 7-day steps so backfill windows line up
    with the raw files landed by capture sweeps; the trailing partial window is clamped
    to ``end`` and refetched as the anchor date advances.
    """

    dataset = "nse.corp_trades_ts"
    _ANCHOR = dt.date(2010, 1, 4)

    def __init__(self, database: Database, source: WindowTradeFetcher | None = None) -> None:
        self._db = database
        self._source = source or NseTradeSettlementSource()

    def run_date(self, date: dt.date) -> PipelineResult:
        """Ingest the anchored window containing ``date`` (clamped at ``date``)."""
        start = self._window_start(date)
        return self._run_window(start, min(start + dt.timedelta(days=6), date))

    def backfill(self, start: dt.date, end: dt.date) -> list[PipelineResult]:
        """Ingest every anchored window overlapping ``[start, end]``."""
        results: list[PipelineResult] = []
        cursor = self._window_start(start)
        while cursor <= end:
            results.append(self._run_window(cursor, min(cursor + dt.timedelta(days=6), end)))
            cursor += dt.timedelta(days=7)
        return results

    # ------------------------------------------------------------------ internals
    def _window_start(self, date: dt.date) -> dt.date:
        offset = (date - self._ANCHOR).days % 7
        return date - dt.timedelta(days=offset)

    def _run_window(self, start: dt.date, end: dt.date) -> PipelineResult:
        def work(session: Session) -> int:
            trades = self._source.fetch_trades(start, end)
            rows = CorporateTradeRepository(session).replace_window(
                self._source.name, start, end, trades
            )
            persist_file_metrics(
                session,
                self._source,
                source=self._source.name,
                dataset=self.dataset,
                run_date=end,
            )
            return rows

        # The run is keyed by the window's end date: one audit row per window.
        return execute_run(
            self._db, source=self._source.name, dataset=self.dataset, run_date=end, work=work
        )
