"""Universe pipeline (pillar 1 + 2): upsert the securities master and track attribute changes.

For a snapshot date it streams every security from a source (BondCentral), upserts identity/​
reference fields into ``securities`` (advancing ``last_seen``), and records changed trackable
attributes (e.g. credit rating) into ``security_attribute_history`` (SCD-2).

Idempotent: re-running a date refreshes rows and records no spurious attribute changes.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Protocol

from sqlalchemy.orm import Session

from bonds.logging import get_logger
from bonds.models import SecurityRecord
from bonds.pipelines.base import PipelineResult, execute_run
from bonds.quality import QualityInspector
from bonds.sources.bondcentral import BondCentralSource
from bonds.storage import Database
from bonds.storage.repositories import SecurityRepository

logger = get_logger(__name__)

# Attributes surfaced by connectors that we track over time (SCD-2). Only non-null values are
# recorded, so day-1 does not flood the table with "unrated" rows.
TRACKED_ATTRIBUTES: tuple[str, ...] = (
    # BondCentral
    "credit_rating",
    "credit_rating_agency",
    "credit_rating_date",
    "security_status",
    "secured_unsecured",
    # CDSL (change over time / per snapshot)
    "amount_outstanding_cr",
    "amount_issued_cr",
    "payment_frequency",
)


class UniverseFetcher(Protocol):
    """The slice of a source connector this pipeline depends on."""

    @property
    def name(self) -> str:
        """Stable source identifier (read-only; connectors declare it ``Final``)."""
        ...

    def iter_records(
        self, as_of: dt.date, *, size: int = ..., max_pages: int | None = ...
    ) -> Iterator[SecurityRecord]:
        """Yield universe securities for a snapshot date."""
        ...


class UniversePipeline:
    """Ingest a securities-master universe into ``securities`` + attribute history."""

    def __init__(self, database: Database, source: UniverseFetcher | None = None) -> None:
        self._db = database
        self._source = source or BondCentralSource()

    def run(self, as_of: dt.date, *, max_pages: int | None = None) -> PipelineResult:
        """Upsert the full universe as of ``as_of``.

        Args:
            as_of: Snapshot date.
            max_pages: Optional page cap passed through to the source (smoke runs).
        """
        dataset = f"{self._source.name}.universe"

        def work(session: Session) -> int:
            records = list(self._source.iter_records(as_of, max_pages=max_pages))
            # Quality + reconciliation run first: reconciliation compares against the row a
            # *different* source last wrote, before this upsert overwrites it.
            QualityInspector(
                session, source=self._source.name, dataset=dataset, run_date=as_of
            ).inspect_universe(records)
            securities = SecurityRepository(session)
            rows = securities.upsert_many(records, seen_on=as_of)
            self._record_attributes(securities, records, effective=as_of)
            return rows

        return execute_run(
            self._db, source=self._source.name, dataset=dataset, run_date=as_of, work=work
        )

    def _record_attributes(
        self, repo: SecurityRepository, records: list[SecurityRecord], *, effective: dt.date
    ) -> int:
        total = 0
        for attribute in TRACKED_ATTRIBUTES:
            values = {
                r.isin: r.attributes[attribute]
                for r in records
                if r.attributes.get(attribute) is not None
            }
            total += repo.record_attribute_bulk(
                attribute, values, effective=effective, source=self._source.name
            )
        return total
