"""Integration tests for IngestionRunRepository.dataset_progress (needs Postgres)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select

from bonds.pipelines.catchup import series_start
from bonds.storage import Database
from bonds.storage.repositories import IngestionRunRepository
from bonds.storage.schema import IngestionRun

pytestmark = pytest.mark.integration

SOURCE = "fakets"  # sentinel source -> non-destructive
AS_OF = dt.date(2026, 7, 17)
CUTOFF = AS_OF - dt.timedelta(days=3)


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


def test_dataset_progress_counts_skip_surfaces_failure(db: Database) -> None:
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 10), "success")
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 13), "skipped")  # holiday, still "processed"
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 8), "failed")  # must be surfaced for retry
    with db.session() as s:
        progress = IngestionRunRepository(s).dataset_progress(SOURCE, skip_retry_cutoff=CUTOFF)
    p = progress[f"{SOURCE}.trades"]
    assert p.processed_through == dt.date(2026, 7, 13)
    assert p.earliest_failed == dt.date(2026, 7, 8)
    assert p.earliest_recent_skip is None  # 07-13 is before the 07-14 cutoff


def test_dataset_progress_recent_skip_is_surfaced(db: Database) -> None:
    _record(db, f"{SOURCE}.trades", AS_OF, "skipped")  # possibly-premature same-day skip
    with db.session() as s:
        progress = IngestionRunRepository(s).dataset_progress(SOURCE, skip_retry_cutoff=CUTOFF)
    assert progress[f"{SOURCE}.trades"].earliest_recent_skip == AS_OF


def test_dataset_progress_empty_for_unseen_source(db: Database) -> None:
    with db.session() as s:
        assert (
            IngestionRunRepository(s).dataset_progress("never-seen-src", skip_retry_cutoff=CUTOFF)
            == {}
        )


def test_series_start_resumes_after_last_processed(db: Database) -> None:
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 13), "success")
    start = series_start(db, SOURCE, as_of=AS_OF, max_gap_days=30)
    assert start == dt.date(2026, 7, 14)  # day after the last processed date


def test_series_start_retries_failed_day_shadowed_by_later_success(db: Database) -> None:
    # Tue failed, Wed-Fri succeeded: resume must pull back to Tue, not jump to Sat.
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 14), "failed")
    for day in (15, 16, 17):
        _record(db, f"{SOURCE}.trades", dt.date(2026, 7, day), "success")
    assert series_start(db, SOURCE, as_of=AS_OF, max_gap_days=30) == dt.date(2026, 7, 14)


def test_series_start_uses_min_across_datasets(db: Database) -> None:
    # gsec succeeded through the 17th but sdl failed on the 16th: the source-level start must
    # honour the laggard dataset, not the leader.
    _record(db, f"{SOURCE}.gsec", dt.date(2026, 7, 17), "success")
    _record(db, f"{SOURCE}.sdl", dt.date(2026, 7, 15), "success")
    _record(db, f"{SOURCE}.sdl", dt.date(2026, 7, 16), "failed")
    assert series_start(db, SOURCE, as_of=AS_OF, max_gap_days=30) == dt.date(2026, 7, 16)


def test_series_start_retries_recent_skip(db: Database) -> None:
    # A same-day skip (ran before the source published) must be re-attempted by a later run.
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 16), "success")
    _record(db, f"{SOURCE}.trades", AS_OF, "skipped")
    assert series_start(db, SOURCE, as_of=AS_OF, max_gap_days=30) == AS_OF


def test_failed_rerun_does_not_downgrade_success_audit_row(db: Database) -> None:
    # The 21:00 run succeeded (rows in DB); a 23:00 manual re-run failing must not rewrite the
    # audit row to failed/0 — the data is still there and drift baselines need the day.
    _record(db, f"{SOURCE}.trades", AS_OF, "success")
    _record(db, f"{SOURCE}.trades", AS_OF, "failed")
    with db.session() as s:
        row = s.execute(
            select(IngestionRun).where(
                IngestionRun.source == SOURCE, IngestionRun.run_date == AS_OF
            )
        ).scalar_one()
    assert row.status == "success"
    assert row.rows_ingested == 1

    # But a later SUCCESS still overwrites (refresh), and failed -> success upgrades.
    _record(db, f"{SOURCE}.other", AS_OF, "failed")
    _record(db, f"{SOURCE}.other", AS_OF, "success")
    with db.session() as s:
        row = s.execute(
            select(IngestionRun).where(
                IngestionRun.dataset == f"{SOURCE}.other", IngestionRun.run_date == AS_OF
            )
        ).scalar_one()
    assert row.status == "success"


def test_series_start_old_skips_are_terminal(db: Database) -> None:
    # Skips older than the retry window are holidays; they must not be refetched forever.
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 10), "skipped")
    _record(db, f"{SOURCE}.trades", dt.date(2026, 7, 13), "success")
    assert series_start(db, SOURCE, as_of=AS_OF, max_gap_days=30) == dt.date(2026, 7, 14)
