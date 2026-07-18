"""Public-issue pipeline: SEBI corporate-bond primary-market calendar into ``public_issues``.

Idempotent per (company, issue_open, source). Runs quality checks each ingest.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from sqlalchemy.orm import Session

from bonds.models import PublicIssueRecord
from bonds.pipelines.base import PipelineResult, execute_run
from bonds.quality import QualityInspector
from bonds.sources.sebi import SebiSource
from bonds.storage import Database
from bonds.storage.repositories import PublicIssueRepository


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

        def work(session: Session) -> int:
            issues = self._source.fetch_public_issues(as_of)
            QualityInspector(
                session, source=self._source.name, dataset=dataset, run_date=as_of
            ).inspect_public_issues(issues)
            return PublicIssueRepository(session).upsert_many(issues)

        return execute_run(
            self._db, source=self._source.name, dataset=dataset, run_date=as_of, work=work
        )
