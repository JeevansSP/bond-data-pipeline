"""Trade pipeline: secondary-market trade summaries into ``trades`` (NSE corporate bonds).

Forward capture (each run snapshots the latest session). Idempotent per
(isin, trade_date, source, segment).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.orm import Session

from bonds.models import SecurityRecord, TradeRecord
from bonds.pipelines.base import PipelineResult, execute_run, persist_file_metrics
from bonds.quality import QualityInspector
from bonds.sources.nse import NseSource
from bonds.storage import Database
from bonds.storage.repositories import SecurityRepository, TradeRepository

SecuritiesDeriver = Callable[[list[TradeRecord]], list[SecurityRecord]]


class TradeFetcher(Protocol):
    """The slice of a source connector this pipeline depends on."""

    @property
    def name(self) -> str:
        """Stable source identifier (read-only; connectors declare it ``Final``)."""
        ...

    def fetch_trades(self, as_of: dt.date) -> list[TradeRecord]:
        """Fetch + parse the latest session's trades."""
        ...


class TradePipeline:
    """Ingest secondary-market trade summaries into ``trades``.

    An optional ``derive_securities`` hook lets a sovereign source (CCIL) also populate the
    ``securities`` master with reference rows for the instruments it trades — T-Bills, STRIPS,
    SGBs and matured G-Secs/SDLs that no universe source covers — inserting only missing ISINs so
    authoritative FBIL/BondCentral rows are never overwritten.
    """

    def __init__(
        self,
        database: Database,
        source: TradeFetcher | None = None,
        *,
        derive_securities: SecuritiesDeriver | None = None,
    ) -> None:
        self._db = database
        self._source = source or NseSource()
        self._derive_securities = derive_securities

    def run(self, as_of: dt.date) -> PipelineResult:
        """Fetch + upsert the latest trades as of ``as_of``."""
        dataset = f"{self._source.name}.trades"

        def work(session: Session) -> int:
            trades = self._source.fetch_trades(as_of)
            QualityInspector(
                session, source=self._source.name, dataset=dataset, run_date=as_of
            ).inspect_trades(trades)
            rows = TradeRepository(session).upsert_many(trades)
            if self._derive_securities is not None:
                SecurityRepository(session).insert_missing(
                    self._derive_securities(trades), seen_on=as_of
                )
            persist_file_metrics(
                session, self._source, source=self._source.name, dataset=dataset, run_date=as_of
            )
            return rows

        return execute_run(
            self._db, source=self._source.name, dataset=dataset, run_date=as_of, work=work
        )
