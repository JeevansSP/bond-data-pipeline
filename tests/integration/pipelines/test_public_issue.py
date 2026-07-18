"""Integration tests for the SEBI public-issue pipeline (needs Postgres; ``-m integration``)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, func, select

from bonds.models import PublicIssueRecord
from bonds.pipelines import PublicIssuePipeline, RunStatus
from bonds.storage import Database, IngestionRun, PublicIssue

pytestmark = pytest.mark.integration

SOURCE = "faketest"
AS_OF = dt.date(2026, 7, 18)


class FakeSebiSource:
    """A source returning canned public-issue records (no network)."""

    name = SOURCE

    def fetch_public_issues(self, as_of: dt.date) -> list[PublicIssueRecord]:
        return [
            PublicIssueRecord(
                company="FAKETEST ALPHA LTD",
                issue_open=dt.date(2025, 5, 1),
                source=SOURCE,
                issue_close=dt.date(2025, 5, 10),
                base_size_cr=100.0,
                final_size_cr=250.0,
                financial_year="2025-26",
            ),
            PublicIssueRecord(
                company="FAKETEST BETA LTD",
                issue_open=dt.date(2025, 6, 1),
                source=SOURCE,
                base_size_cr=50.0,
                final_size_cr=50.0,
                financial_year="2025-26",
            ),
        ]


@pytest.fixture
def database() -> Database:
    db = Database()
    db.create_all()
    return db


@pytest.fixture(autouse=True)
def _cleanup(database: Database) -> Iterator[None]:
    def wipe() -> None:
        with database.session() as s:
            s.execute(delete(PublicIssue).where(PublicIssue.source == SOURCE))
            s.execute(delete(IngestionRun).where(IngestionRun.source == SOURCE))

    wipe()
    yield
    wipe()


def test_run_persists_public_issues(database: Database) -> None:
    result = PublicIssuePipeline(database, source=FakeSebiSource()).run(AS_OF)
    assert result.status is RunStatus.SUCCESS
    assert result.rows == 2
    with database.session() as s:
        companies = (
            s.execute(select(PublicIssue.company).where(PublicIssue.source == SOURCE))
            .scalars()
            .all()
        )
    assert set(companies) == {"FAKETEST ALPHA LTD", "FAKETEST BETA LTD"}


def test_run_is_idempotent(database: Database) -> None:
    pipeline = PublicIssuePipeline(database, source=FakeSebiSource())
    pipeline.run(AS_OF)
    pipeline.run(AS_OF)
    with database.session() as s:
        count = s.execute(
            select(func.count()).select_from(PublicIssue).where(PublicIssue.source == SOURCE)
        ).scalar_one()
    assert count == 2  # upsert, not duplicate
