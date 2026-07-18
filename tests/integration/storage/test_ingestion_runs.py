"""Integration tests for IngestionRunRepository.last_processed_date (needs Postgres)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete

from bonds.pipelines.catchup import series_start
from bonds.storage import Database
from bonds.storage.repositories import IngestionRunRepository
from bonds.storage.schema import IngestionRun

pytestmark = pytest.mark.integration

SOURCE = "fakets"  # sentinel source -> non-destructive


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database()
    database.create_all()

    def _clean() -> None:
        with database.session() as s:
            s.execute(delete(IngestionRun).where(IngestionRun.source == SOURCE))

    _clean()
    yield database
    _clean()


def _record(db: Database, dataset: str, run_date: dt.date, status: str) -> None:
    with db.session() as s:
        IngestionRunRepository(s).record(
            source=SOURCE,
            dataset=dataset,
            run_date=run_date,
            status=status,
            rows=1,
            started_at=dt.datetime.now(dt.UTC),
        )


def test_last_processed_date_counts_skip_ignores_failure(db: Database) -> None:
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 10), "success")
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 13), "skipped")  # holiday, still "processed"
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 14), "failed")  # must NOT anchor here
    with db.session() as s:
        anchor = IngestionRunRepository(s).last_processed_date(SOURCE)
    assert anchor == dt.date(2026, 7, 13)


def test_last_processed_date_none_for_unseen_source(db: Database) -> None:
    with db.session() as s:
        assert IngestionRunRepository(s).last_processed_date("never-seen-src") is None


def test_series_start_resumes_after_last_processed(db: Database) -> None:
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 13), "success")
    start = series_start(db, SOURCE, as_of=dt.date(2026, 7, 17), max_gap_days=30)
    assert start == dt.date(2026, 7, 14)  # day after the last processed date
