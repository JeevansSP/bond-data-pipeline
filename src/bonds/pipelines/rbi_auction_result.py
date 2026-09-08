"""RBI auction-result pipeline: primary-market cut-offs into ``rbi_auction_results``.

Runs off the calendar: the auction index is fetched, the "Full Auction Result" releases in it
are identified by title, and each is fetched and parsed into a record per security. Idempotent
per (prid, security) — re-running a day refreshes rather than duplicating, which matters because
RBI occasionally reposts a release with a correction.
"""

from __future__ import annotations

import datetime as dt
from typing import Final, Protocol

from sqlalchemy.orm import Session

from bonds.models import RbiAuctionRecord, RbiAuctionResultRecord
from bonds.pipelines.base import PipelineResult, execute_run, persist_file_metrics
from bonds.sources.base import DataUnavailable
from bonds.sources.rbi import RbiSource
from bonds.storage import Database
from bonds.storage.repositories import RbiAuctionResultRepository


class AuctionResultFetcher(Protocol):
    """The slice of a source connector this pipeline depends on."""

    @property
    def name(self) -> str:
        """Stable source identifier (read-only; connectors declare it ``Final``)."""
        ...

    def fetch_auctions(self, as_of: dt.date) -> list[RbiAuctionRecord]:
        """Fetch + parse the auction calendar."""
        ...

    def fetch_auction_results(
        self, auctions: list[RbiAuctionRecord]
    ) -> list[RbiAuctionResultRecord]:
        """Fetch + parse the full-result release behind each result announcement."""
        ...


class RbiAuctionResultPipeline:
    """Ingest per-security RBI auction outcomes into ``rbi_auction_results``."""

    dataset: Final = "rbi.auction_results"

    def __init__(self, database: Database, source: AuctionResultFetcher | None = None) -> None:
        self._db = database
        self._source = source or RbiSource()

    def run(self, as_of: dt.date) -> PipelineResult:
        """Fetch the current index and upsert every full auction result it lists.

        Raises:
            DataUnavailable: When the index carries no full-result release — a day with no
                auction, which is most of them. Recorded SKIPPED so it is re-attempted rather
                than banked as a processed day.
        """
        dataset = self.dataset

        def work(session: Session) -> int:
            auctions = self._source.fetch_auctions(as_of)
            results = self._source.fetch_auction_results(auctions)
            if not results:
                raise DataUnavailable(f"no full auction results published as of {as_of}")
            rows = RbiAuctionResultRepository(session).upsert_many(results)
            persist_file_metrics(
                session, self._source, source=self._source.name, dataset=dataset, run_date=as_of
            )
            return rows

        return execute_run(
            self._db, source=self._source.name, dataset=dataset, run_date=as_of, work=work
        )
