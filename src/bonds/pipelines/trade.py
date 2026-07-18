"""Trade pipeline: secondary-market trade summaries into ``trades`` (NSE corporate bonds).

Forward capture (each run snapshots the latest session). Idempotent per
(isin, trade_date, source, segment).
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from bonds.logging import get_logger
from bonds.models import TradeRecord
from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.quality import QualityInspector
from bonds.sources.nse import NseSource
from bonds.storage import Database
from bonds.storage.repositories import IngestionRunRepository, TradeRepository

logger = get_logger(__name__)


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
    """Ingest secondary-market trade summaries into ``trades``."""

    def __init__(self, database: Database, source: TradeFetcher | None = None) -> None:
        self._db = database
        self._source = source or NseSource()

    def run(self, as_of: dt.date) -> PipelineResult:
        """Fetch + upsert the latest trades as of ``as_of``."""
        dataset = f"{self._source.name}.trades"
        with self._db.session() as session:
            runs = IngestionRunRepository(session)
            run = runs.start(source=self._source.name, dataset=dataset, run_date=as_of)
            try:
                trades = self._source.fetch_trades(as_of)
            except Exception as exc:  # audit then surface as FAILED result
                runs.finish(run, status=RunStatus.FAILED, message=repr(exc))
                logger.error("trade.failed", dataset=dataset, error=repr(exc))
                return PipelineResult(as_of, dataset, RunStatus.FAILED, message=repr(exc))

            QualityInspector(
                session, source=self._source.name, dataset=dataset, run_date=as_of
            ).inspect_trades(trades)
            rows = TradeRepository(session).upsert_many(trades)
            runs.finish(run, status=RunStatus.SUCCESS, rows=rows)
            logger.info("trade.success", dataset=dataset, rows=rows)
            return PipelineResult(as_of, dataset, RunStatus.SUCCESS, rows=rows)
