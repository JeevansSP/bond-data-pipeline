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

from bonds.logging import get_logger
from bonds.models import SecurityRecord
from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.sources.bondcentral import BondCentralSource
from bonds.storage import Database
from bonds.storage.repositories import (
    IngestionRunRepository,
    SecurityRepository,
)

logger = get_logger(__name__)

# Attributes surfaced by connectors that we track over time (SCD-2). Only non-null values are
# recorded, so day-1 does not flood the table with "unrated" rows.
TRACKED_ATTRIBUTES: tuple[str, ...] = ("credit_rating", "security_status", "secured_unsecured")


class UniverseFetcher(Protocol):
    """The slice of a source connector this pipeline depends on."""

    name: str

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
        with self._db.session() as session:
            runs = IngestionRunRepository(session)
            run = runs.start(source=self._source.name, dataset=dataset, run_date=as_of)
            try:
                records = list(self._source.iter_records(as_of, max_pages=max_pages))
            except Exception as exc:  # audit then surface as FAILED result
                runs.finish(run, status=RunStatus.FAILED, message=repr(exc))
                logger.error("universe.failed", dataset=dataset, error=repr(exc))
                return PipelineResult(as_of, dataset, RunStatus.FAILED, message=repr(exc))

            securities = SecurityRepository(session)
            rows = securities.upsert_many(records, seen_on=as_of)
            changes = self._record_attributes(securities, records, effective=as_of)
            runs.finish(run, status=RunStatus.SUCCESS, rows=rows, message=f"{changes} attr changes")
            logger.info("universe.success", dataset=dataset, rows=rows, attr_changes=changes)
            return PipelineResult(as_of, dataset, RunStatus.SUCCESS, rows=rows)

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
