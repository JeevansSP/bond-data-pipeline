"""Yield-curve pipeline: FBIL published curves (G-Sec Par Yield, GOI ZCYC, SDL ZCYC).

The curves ride inside the FBIL product workbooks (`gsec` sheet "Par Yield", `strips` sheet
"ZCYC", `sdlzcyc` sheet "SDL_ZCYC"), so ingestion is lake-first: a workbook already landed by
the valuation pipeline or a raw backfill is parsed without re-downloading.

Idempotent and append-only: re-running a date with identical values writes nothing; a
restated curve closes the current rows (``superseded_at``) and inserts fresh versions.
Missing days (HTTP 500 = non-publishing day) are recorded as ``skipped``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.orm import Session

from bonds.calendar import business_days
from bonds.models import YieldCurvePoint
from bonds.pipelines.base import PipelineResult, execute_run, persist_file_metrics
from bonds.sources.fbil import FbilSource
from bonds.storage import Database
from bonds.storage.repositories import YieldCurveRepository

DEFAULT_CURVE_PRODUCTS: tuple[str, ...] = ("gsec", "strips", "sdlzcyc")


class CurveFetcher(Protocol):
    """The slice of a source connector this pipeline depends on."""

    @property
    def name(self) -> str:
        """Stable source identifier (read-only; connectors declare it ``Final``)."""
        ...

    def fetch_curves(self, product: str, date: dt.date) -> list[YieldCurvePoint]:
        """Fetch + parse one product/date's published yield-curve sheet."""
        ...


class YieldCurvePipeline:
    """Ingest FBIL published yield curves into ``yield_curves``."""

    def __init__(
        self,
        database: Database,
        source: CurveFetcher | None = None,
        products: Sequence[str] = DEFAULT_CURVE_PRODUCTS,
    ) -> None:
        self._db = database
        self._source = source or FbilSource()
        self._products = tuple(products)

    def run_date(self, date: dt.date) -> list[PipelineResult]:
        """Ingest every configured curve product for a single business date."""
        return [self._run_product(product, date) for product in self._products]

    def backfill(self, start: dt.date, end: dt.date) -> list[PipelineResult]:
        """Ingest every configured product across ``[start, end]`` (Mon-Sat; Sundays excluded)."""
        results: list[PipelineResult] = []
        for day in business_days(start, end):
            results.extend(self.run_date(day))
        return results

    # ------------------------------------------------------------------ internals
    def _run_product(self, product: str, date: dt.date) -> PipelineResult:
        dataset = f"{self._source.name}.curve.{product}"

        def work(session: Session) -> int:
            # fetch_curves raises DataUnavailable on a holiday -> execute_run -> SKIPPED.
            points = self._source.fetch_curves(product, date)
            rows = YieldCurveRepository(session).upsert_many(points)
            persist_file_metrics(
                session, self._source, source=self._source.name, dataset=dataset, run_date=date
            )
            return rows

        return execute_run(
            self._db, source=self._source.name, dataset=dataset, run_date=date, work=work
        )
