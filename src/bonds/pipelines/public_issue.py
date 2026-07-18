"""Public-issue pipeline: SEBI corporate-bond primary-market calendar into ``public_issues``.

Idempotent per (company, issue_open, source). Runs quality checks each ingest.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from bonds.logging import get_logger
from bonds.models import PublicIssueRecord
from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.quality import QualityInspector
from bonds.sources.sebi import SebiSource
from bonds.storage import Database
from bonds.storage.repositories import IngestionRunRepository, PublicIssueRepository

logger = get_logger(__name__)


class PublicIssueFetcher(Protocol):
    """The slice of a source connector this pipeline depends on."""

    @property
    def name(self) -> str:
        """Stable source identifier (read-only; connectors declare it ``Final``)."""
        ...

    def fetch_public_issues(self, as_of: dt.date) -> list[PublicIssueRecord]:
        """Fetch + parse the public-issue calendar."""
        ...


class PublicIssuePipeline:
    """Ingest the SEBI public-issue calendar into ``public_issues``."""

    def __init__(self, database: Database, source: PublicIssueFetcher | None = None) -> None:
        self._db = database
        self._source = source or SebiSource()

    def run(self, as_of: dt.date) -> PipelineResult:
        """Fetch + upsert the full public-issue calendar as of ``as_of``."""
        dataset = f"{self._source.name}.public_issues"
        with self._db.session() as session:
            runs = IngestionRunRepository(session)
            run = runs.start(source=self._source.name, dataset=dataset, run_date=as_of)
            try:
                issues = self._source.fetch_public_issues(as_of)
            except Exception as exc:  # audit then surface as FAILED result
                runs.finish(run, status=RunStatus.FAILED, message=repr(exc))
                logger.error("public_issue.failed", dataset=dataset, error=repr(exc))
                return PipelineResult(as_of, dataset, RunStatus.FAILED, message=repr(exc))

            QualityInspector(
                session, source=self._source.name, dataset=dataset, run_date=as_of
            ).inspect_public_issues(issues)
            rows = PublicIssueRepository(session).upsert_many(issues)
            runs.finish(run, status=RunStatus.SUCCESS, rows=rows)
            logger.info("public_issue.success", dataset=dataset, rows=rows)
            return PipelineResult(as_of, dataset, RunStatus.SUCCESS, rows=rows)
