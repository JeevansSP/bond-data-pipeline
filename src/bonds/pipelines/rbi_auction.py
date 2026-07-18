"""RBI auction pipeline: sovereign auction calendar into ``rbi_auctions``.

Idempotent per (prid, source). Runs quality checks each ingest.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from bonds.logging import get_logger
from bonds.models import RbiAuctionRecord
from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.quality import QualityInspector
from bonds.sources.rbi import RbiSource
from bonds.storage import Database
from bonds.storage.repositories import IngestionRunRepository, RbiAuctionRepository

logger = get_logger(__name__)


class AuctionFetcher(Protocol):
    """The slice of a source connector this pipeline depends on."""

    @property
    def name(self) -> str:
        """Stable source identifier (read-only; connectors declare it ``Final``)."""
        ...

    def fetch_auctions(self, as_of: dt.date) -> list[RbiAuctionRecord]:
        """Fetch + parse the auction calendar."""
        ...


class RbiAuctionPipeline:
    """Ingest the RBI sovereign auction calendar into ``rbi_auctions``."""

    def __init__(self, database: Database, source: AuctionFetcher | None = None) -> None:
        self._db = database
        self._source = source or RbiSource()

    def run(self, as_of: dt.date) -> PipelineResult:
        """Fetch + upsert the auction calendar as of ``as_of``."""
        dataset = f"{self._source.name}.auctions"
        with self._db.session() as session:
            runs = IngestionRunRepository(session)
            run = runs.start(source=self._source.name, dataset=dataset, run_date=as_of)
            try:
                auctions = self._source.fetch_auctions(as_of)
            except Exception as exc:  # audit then surface as FAILED result
                runs.finish(run, status=RunStatus.FAILED, message=repr(exc))
                logger.error("rbi_auction.failed", dataset=dataset, error=repr(exc))
                return PipelineResult(as_of, dataset, RunStatus.FAILED, message=repr(exc))

            QualityInspector(
                session, source=self._source.name, dataset=dataset, run_date=as_of
            ).inspect_rbi_auctions(auctions)
            rows = RbiAuctionRepository(session).upsert_many(auctions, seen_on=as_of)
            runs.finish(run, status=RunStatus.SUCCESS, rows=rows)
            logger.info("rbi_auction.success", dataset=dataset, rows=rows)
            return PipelineResult(as_of, dataset, RunStatus.SUCCESS, rows=rows)
