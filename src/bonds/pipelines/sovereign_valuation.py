"""Sovereign valuation pipeline (pillar 3): FBIL G-Sec/SDL daily price & YTM.

For each product/date it:
    1. downloads + parses the FBIL published workbook (raw file landed in the data lake),
    2. upserts the valuations (price/YTM history),
    3. upserts the securities it references into the universe (pillar 1 for sovereigns),
    4. writes an ingestion audit row.

Idempotent: re-running a date refreshes rather than duplicates. Missing days (HTTP 500) are
recorded as ``skipped`` and never abort a backfill.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.orm import Session

from bonds.calendar import business_days
from bonds.logging import get_logger
from bonds.models import InstrumentType, SecurityRecord, SovereignValuation
from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.quality import QualityInspector
from bonds.sources.base import DataUnavailable
from bonds.sources.fbil import FbilSource
from bonds.storage import Database
from bonds.storage.repositories import (
    IngestionRunRepository,
    SecurityRepository,
    ValuationRepository,
)

logger = get_logger(__name__)

DEFAULT_PRODUCTS: tuple[str, ...] = ("gsec", "sdl")


class ValuationFetcher(Protocol):
    """The slice of a source connector this pipeline depends on."""

    @property
    def name(self) -> str:
        """Stable source identifier (read-only; connectors declare it ``Final``)."""
        ...

    def fetch_valuations(self, product: str, date: dt.date) -> list[SovereignValuation]:
        """Download + parse one product/date into valuation records."""
        ...


class SovereignValuationPipeline:
    """Ingest FBIL sovereign valuations into ``valuations`` + ``securities``."""

    def __init__(
        self,
        database: Database,
        source: ValuationFetcher | None = None,
        products: Sequence[str] = DEFAULT_PRODUCTS,
    ) -> None:
        self._db = database
        self._source = source or FbilSource()
        self._products = tuple(products)

    def run_date(self, date: dt.date) -> list[PipelineResult]:
        """Ingest every configured product for a single business date."""
        return [self._run_product(product, date) for product in self._products]

    def backfill(self, start: dt.date, end: dt.date) -> list[PipelineResult]:
        """Ingest every configured product across ``[start, end]`` (weekdays only)."""
        results: list[PipelineResult] = []
        for day in business_days(start, end):
            results.extend(self.run_date(day))
        return results

    # ------------------------------------------------------------------ internals
    def _run_product(self, product: str, date: dt.date) -> PipelineResult:
        dataset = f"{self._source.name}.{product}"
        with self._db.session() as session:
            runs = IngestionRunRepository(session)
            run = runs.start(source=self._source.name, dataset=dataset, run_date=date)
            try:
                valuations = self._source.fetch_valuations(product, date)
            except DataUnavailable as exc:
                runs.finish(run, status=RunStatus.SKIPPED, message=str(exc))
                logger.info("pipeline.skipped", dataset=dataset, date=date.isoformat())
                return PipelineResult(date, dataset, RunStatus.SKIPPED, message=str(exc))
            except Exception as exc:
                runs.finish(run, status=RunStatus.FAILED, message=repr(exc))
                logger.error(
                    "pipeline.failed", dataset=dataset, date=date.isoformat(), error=repr(exc)
                )
                return PipelineResult(date, dataset, RunStatus.FAILED, message=repr(exc))

            rows = self._persist(session, valuations, seen_on=date)
            QualityInspector(
                session, source=self._source.name, dataset=dataset, run_date=date
            ).inspect_valuations(valuations)
            runs.finish(run, status=RunStatus.SUCCESS, rows=rows)
            logger.info("pipeline.success", dataset=dataset, date=date.isoformat(), rows=rows)
            return PipelineResult(date, dataset, RunStatus.SUCCESS, rows=rows)

    @staticmethod
    def _persist(
        session: Session, valuations: list[SovereignValuation], *, seen_on: dt.date
    ) -> int:
        ValuationRepository(session).upsert_many(valuations)
        securities = [_to_security(v) for v in valuations]
        SecurityRepository(session).upsert_many(securities, seen_on=seen_on)
        return len(valuations)


# Government securities carry a standard ₹100 face value, which FBIL files omit; default it so
# downstream cashflow math has a value rather than a null.
_SOVEREIGN_FACE_VALUE = 100.0


def _to_security(v: SovereignValuation) -> SecurityRecord:
    """Derive a universe :class:`SecurityRecord` from a valuation row, enriching identity."""
    return SecurityRecord(
        isin=v.isin,
        instrument_type=v.instrument_type,
        source=v.source,
        description=v.description,
        issuer=_sovereign_issuer(v),
        coupon=v.coupon,
        interest_type="Zero" if v.coupon in (None, 0.0) else "Fixed",
        maturity_date=v.maturity_date,
        face_value=_SOVEREIGN_FACE_VALUE,
    )


def _sovereign_issuer(v: SovereignValuation) -> str:
    """Derive the issuer: GoI for G-Secs; the issuing state (from the description) for SDLs."""
    if v.instrument_type is InstrumentType.SDL and v.description:
        # SDL descriptions look like "07.83 GJ SDL 2026" -> state code is the 2nd token.
        parts = v.description.split()
        if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
            return f"State Government ({parts[1].upper()})"
    return "Government of India"
