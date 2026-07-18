"""RBI auction pipeline: sovereign auction calendar into ``rbi_auctions``.

Idempotent per (prid, source). Runs quality checks each ingest.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from sqlalchemy.orm import Session

from bonds.models import RbiAuctionRecord
from bonds.pipelines.base import PipelineResult, execute_run
from bonds.quality import QualityInspector
from bonds.sources.rbi import RbiSource
from bonds.storage import Database
from bonds.storage.repositories import RbiAuctionRepository


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

        def work(session: Session) -> int:
            auctions = self._source.fetch_auctions(as_of)
            QualityInspector(
                session, source=self._source.name, dataset=dataset, run_date=as_of
            ).inspect_rbi_auctions(auctions)
            return RbiAuctionRepository(session).upsert_many(auctions, seen_on=as_of)

        return execute_run(
            self._db, source=self._source.name, dataset=dataset, run_date=as_of, work=work
        )
