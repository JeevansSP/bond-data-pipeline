"""The end-to-end ingest suite: every daily pipeline as an ordered list of steps.

Kept UI-free (no rich) so the orchestration is testable; the CLI renders it with a live TUI.
CDSL is excluded from the daily suite — it is a half-yearly snapshot ingested on demand.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.pipelines.public_issue import PublicIssuePipeline
from bonds.pipelines.rbi_auction import RbiAuctionPipeline
from bonds.pipelines.sovereign_valuation import SovereignValuationPipeline
from bonds.pipelines.trade import TradePipeline
from bonds.pipelines.universe import UniversePipeline
from bonds.sources.ccil import CcilSource
from bonds.sources.nse import NseSource
from bonds.storage import Database


@dataclass(frozen=True, slots=True)
class IngestStep:
    """One labelled step of the daily suite; ``run`` performs it and returns its results."""

    label: str
    run: Callable[[], list[PipelineResult]]


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Aggregated result of a step (a step may run several datasets, e.g. FBIL gsec+sdl)."""

    ok: int
    skipped: int
    failed: int
    rows: int

    @property
    def has_failure(self) -> bool:
        """Whether any dataset in the step failed."""
        return self.failed > 0


def summarize(results: list[PipelineResult]) -> StepOutcome:
    """Fold a step's per-dataset results into a single :class:`StepOutcome`."""
    return StepOutcome(
        ok=sum(1 for r in results if r.status is RunStatus.SUCCESS),
        skipped=sum(1 for r in results if r.status is RunStatus.SKIPPED),
        failed=sum(1 for r in results if r.status is RunStatus.FAILED),
        rows=sum(r.rows for r in results),
    )


def default_suite(
    database: Database, as_of: dt.date, *, max_universe_pages: int | None = None
) -> list[IngestStep]:
    """Build the ordered daily ingest suite for ``as_of``."""
    return [
        IngestStep(
            "Universe · BondCentral",
            lambda: [UniversePipeline(database).run(as_of, max_pages=max_universe_pages)],
        ),
        IngestStep(
            "Sovereign valuations · FBIL",
            lambda: SovereignValuationPipeline(database).run_date(as_of),
        ),
        IngestStep(
            "Public issues · SEBI",
            lambda: [PublicIssuePipeline(database).run(as_of)],
        ),
        IngestStep(
            "Auctions · RBI",
            lambda: [RbiAuctionPipeline(database).run(as_of)],
        ),
        IngestStep(
            "Corp trades · NSE",
            lambda: [TradePipeline(database, source=NseSource()).run(as_of)],
        ),
        IngestStep(
            "G-Sec trades · CCIL",
            lambda: [TradePipeline(database, source=CcilSource()).run(as_of)],
        ),
    ]
