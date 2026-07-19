"""Enrichment pipeline: fill missing reference fields on securities from BondCentral.

Some universe sources (notably CDSL) leave `coupon`/`issuer`/`maturity` sparse. BondCentral's
per-ISIN detail lookup carries them, so this pass fetches reference data for securities that are
missing a coupon and coalesce-fills the gaps — it never overwrites a value another source set.

Network fetches happen outside the DB transaction; only the coalesce-update is transactional and
audited (as ``bondcentral.enrichment``). Idempotent: a re-run only revisits still-missing rows.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from bonds.logging import get_logger
from bonds.models import SecurityRecord
from bonds.pipelines.base import PipelineResult, execute_run
from bonds.sources.base import SourceError
from bonds.sources.bondcentral import BondCentralSource
from bonds.storage import Database
from bonds.storage.repositories import SecurityRepository
from bonds.storage.schema import Security

logger = get_logger(__name__)


class ReferenceFetcher(Protocol):
    """The slice of a connector this pipeline needs."""

    @property
    def name(self) -> str:
        """Stable source identifier."""
        ...

    def fetch_reference(self, isin: str) -> SecurityRecord | None:
        """Fetch one security's reference data by ISIN, or ``None`` if not covered."""
        ...


class EnrichmentPipeline:
    """Coalesce-fill missing securities reference fields from a reference source."""

    def __init__(self, database: Database, source: ReferenceFetcher | None = None) -> None:
        self._db = database
        self._source = source or BondCentralSource()

    def run(self, as_of: dt.date, *, limit: int | None = None) -> PipelineResult:
        """Enrich up to ``limit`` securities that are missing a coupon (all if ``None``)."""
        dataset = f"{self._source.name}.enrichment"
        with self._db.session() as session:
            # BondCentral is a corporate securities master, so only corporate rows are enrichable;
            # sovereign ISINs (IN00...) aren't in it and would all resolve to None.
            stmt = (
                select(Security.isin)
                .where(Security.coupon.is_(None), Security.instrument_type == "CORP")
                .order_by(Security.isin)
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            isins = list(session.execute(stmt).scalars())

        records: list[SecurityRecord] = []
        for isin in isins:
            try:
                record = self._source.fetch_reference(isin)
            except (SourceError, httpx.HTTPError) as exc:
                logger.warning("enrichment.fetch_failed", isin=isin, error=str(exc))
                continue
            if record is not None:
                records.append(record)
        logger.info("enrichment.fetched", requested=len(isins), resolved=len(records))

        def work(session: Session) -> int:
            return SecurityRepository(session).enrich_missing(records)

        return execute_run(
            self._db, source=self._source.name, dataset=dataset, run_date=as_of, work=work
        )
