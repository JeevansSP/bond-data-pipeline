"""Integration tests for the universe pipeline (needs Postgres; ``-m integration``).

Uses sentinel ISINs (``INUNIV______``) and source ``fakeuniv`` so it is non-destructive.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select, text

from bonds.models import InstrumentType, SecurityRecord
from bonds.pipelines import RunStatus, UniversePipeline
from bonds.storage import (
    Database,
    IngestionRun,
    Security,
    SecurityAttributeHistory,
)
from bonds.storage.schema import Valuation

pytestmark = pytest.mark.integration

SOURCE = "fakeuniv"
ISIN_A = "INUNIV000001"
ISIN_B = "INUNIV000002"
DAY1 = dt.date(2026, 7, 10)
DAY2 = dt.date(2026, 7, 17)


class FakeUniverseSource:
    """A :class:`UniverseFetcher` yielding canned records (no network)."""

    def __init__(self, records: list[SecurityRecord], *, name: str = SOURCE) -> None:
        self.name = name
        self._records = records

    def iter_records(
        self, as_of: dt.date, *, size: int = 100, max_pages: int | None = None
    ) -> Iterator[SecurityRecord]:
        yield from self._records


def _rec(
    isin: str,
    rating: str | None,
    *,
    maturity: dt.date | None = None,
    status: str = "ACTIVE",
) -> SecurityRecord:
    return SecurityRecord(
        isin=isin,
        instrument_type=InstrumentType.CORP,
        source=SOURCE,
        issuer="ACME LTD",
        coupon=7.0,
        maturity_date=maturity,
        attributes={"credit_rating": rating, "security_status": status},
    )


@pytest.fixture
def database() -> Database:
    db = Database()
    db.create_all()
    return db


@pytest.fixture(autouse=True)
def _cleanup(database: Database) -> Iterator[None]:
    def wipe() -> None:
        with database.session() as s:
            s.execute(delete(Valuation).where(Valuation.source == SOURCE))
            s.execute(delete(Security).where(Security.source == SOURCE))
            s.execute(
                delete(SecurityAttributeHistory).where(SecurityAttributeHistory.source == SOURCE)
            )
            s.execute(delete(IngestionRun).where(IngestionRun.source == SOURCE))

    wipe()
    yield
    wipe()


def _rating_history(database: Database, isin: str) -> list[tuple[str | None, dt.date | None]]:
    with database.session() as s:
        rows = (
            s.execute(
                select(SecurityAttributeHistory)
                .where(
                    SecurityAttributeHistory.isin == isin,
                    SecurityAttributeHistory.attribute == "credit_rating",
                )
                .order_by(SecurityAttributeHistory.valid_from)
            )
            .scalars()
            .all()
        )
    return [(r.value, r.valid_to) for r in rows]


def test_upserts_universe_and_records_ratings(database: Database) -> None:
    source = FakeUniverseSource([_rec(ISIN_A, "AAA"), _rec(ISIN_B, "AA+")])
    result = UniversePipeline(database, source=source).run(DAY1)

    assert result.status is RunStatus.SUCCESS
    assert result.rows == 2
    with database.session() as s:
        secs = s.execute(select(Security).where(Security.source == SOURCE)).scalars().all()
    assert {sec.isin for sec in secs} == {ISIN_A, ISIN_B}
    assert _rating_history(database, ISIN_A) == [("AAA", None)]


def test_rerun_is_idempotent_no_spurious_changes(database: Database) -> None:
    source = FakeUniverseSource([_rec(ISIN_A, "AAA"), _rec(ISIN_B, "AA+")])
    pipeline = UniversePipeline(database, source=source)
    pipeline.run(DAY1)
    pipeline.run(DAY1)  # same snapshot again
    assert _rating_history(database, ISIN_A) == [("AAA", None)]  # still one row


def test_rating_downgrade_is_recorded_as_scd2(database: Database) -> None:
    UniversePipeline(database, source=FakeUniverseSource([_rec(ISIN_A, "AAA")])).run(DAY1)
    UniversePipeline(database, source=FakeUniverseSource([_rec(ISIN_A, "AA")])).run(DAY2)

    assert _rating_history(database, ISIN_A) == [
        ("AAA", DAY2 - dt.timedelta(days=1)),  # closed the day before the change
        ("AA", None),  # current
    ]


def test_quality_checks_are_persisted(database: Database) -> None:
    source = FakeUniverseSource([_rec(ISIN_A, "AAA"), _rec(ISIN_B, "AA+")])
    UniversePipeline(database, source=source).run(DAY1)
    with database.session() as s:
        checks = (
            s.execute(
                text(
                    "SELECT check_name, passed FROM data_quality_checks "
                    "WHERE source=:src ORDER BY check_name"
                ),
                {"src": SOURCE},
            )
            .mappings()
            .all()
        )
    names = {c["check_name"] for c in checks}
    assert {"row_count", "invalid_isin", "matured_in_universe", "row_count_drift"} <= names
    # row_count is a real signal here; invalid_isin correctly *flags* the synthetic sentinels.
    assert next(c for c in checks if c["check_name"] == "row_count")["passed"]
    assert not next(c for c in checks if c["check_name"] == "invalid_isin")["passed"]


def test_active_securities_view_excludes_matured_and_dead(database: Database) -> None:
    yesterday = DAY1 - dt.timedelta(days=1)
    future = DAY1 + dt.timedelta(days=365)
    source = FakeUniverseSource(
        [
            _rec(ISIN_A, "AAA", maturity=future, status="ACTIVE"),  # investable
            _rec(ISIN_B, "AAA", maturity=yesterday, status="ACTIVE"),  # matured -> excluded
        ]
    )
    UniversePipeline(database, source=source).run(DAY1)
    with database.session() as s:
        active = (
            s.execute(text("SELECT isin FROM active_securities WHERE source=:src"), {"src": SOURCE})
            .scalars()
            .all()
        )
    assert ISIN_A in active
    assert ISIN_B not in active  # matured is filtered out of the investable universe


def test_same_day_attribute_change_is_idempotent(database: Database) -> None:
    # Two runs on the SAME day where the rating changes must not crash (was IntegrityError).
    UniversePipeline(database, source=FakeUniverseSource([_rec(ISIN_A, "AAA")])).run(DAY1)
    UniversePipeline(database, source=FakeUniverseSource([_rec(ISIN_A, "AA")])).run(DAY1)
    assert _rating_history(database, ISIN_A) == [("AA", None)]  # updated in place, one open row


def test_out_of_order_backfill_preserves_current_value(database: Database) -> None:
    # Ingest a LATER date first, then an EARLIER one: the older observation is skipped rather than
    # overwriting the newer current value or crashing.
    UniversePipeline(database, source=FakeUniverseSource([_rec(ISIN_A, "AAA")])).run(DAY2)
    UniversePipeline(database, source=FakeUniverseSource([_rec(ISIN_A, "AA")])).run(DAY1)
    assert _rating_history(database, ISIN_A) == [("AAA", None)]  # current value intact


def test_multiple_runs_in_a_day_stay_idempotent(database: Database) -> None:
    src = FakeUniverseSource([_rec(ISIN_A, "AAA"), _rec(ISIN_B, "AA+")])
    UniversePipeline(database, source=src).run(DAY1)
    UniversePipeline(database, source=src).run(DAY1)  # re-run same day
    with database.session() as s:
        runs = s.execute(
            text("SELECT count(*) FROM ingestion_runs WHERE source=:x AND run_date=:d"),
            {"x": SOURCE, "d": DAY1},
        ).scalar_one()
        checks = s.execute(
            text(
                "SELECT count(*) FROM data_quality_checks "
                "WHERE source=:x AND run_date=:d AND check_name='row_count'"
            ),
            {"x": SOURCE, "d": DAY1},
        ).scalar_one()
    assert runs == 1  # upserted, not appended
    assert checks == 1


def test_db_phase_failure_is_audited_and_returns_failed(database: Database) -> None:
    # A too-long record source (> securities.source VARCHAR(32)) fails on insert, in the load
    # phase. The pipeline's own source name stays SOURCE, so the audit row is cleaned up.
    bad = SecurityRecord(isin=ISIN_A, instrument_type=InstrumentType.CORP, source="X" * 40)
    result = UniversePipeline(database, source=FakeUniverseSource([bad])).run(DAY1)
    assert result.status is RunStatus.FAILED  # returned, not raised
    with database.session() as s:
        status = s.execute(
            text("SELECT status FROM ingestion_runs WHERE source=:x AND run_date=:d"),
            {"x": SOURCE, "d": DAY1},
        ).scalar_one()
    assert status == "failed"  # audit row survives the rolled-back work transaction


def test_pipeline_persists_etl_file_metrics(database: Database) -> None:
    from bonds.quality.metrics import MetricsCollector

    src_name = "metrictest"

    class MetricFake(MetricsCollector):
        name = src_name

        def __init__(self) -> None:
            self.reset_metrics()

        def iter_records(
            self, as_of: dt.date, *, size: int = 100, max_pages: int | None = None
        ) -> Iterator[SecurityRecord]:
            self.add_metric("art1", bytes_downloaded=123, rows_extracted=1, rows_parsed=1)
            yield SecurityRecord(
                isin="INUNIV000007", instrument_type=InstrumentType.CORP, source=src_name
            )

    try:
        UniversePipeline(database, source=MetricFake()).run(DAY1)
        with database.session() as s:
            row = s.execute(
                text(
                    "SELECT artifact, bytes_downloaded, rows_parsed "
                    "FROM etl_file_metrics WHERE source=:x"
                ),
                {"x": src_name},
            ).one()
        assert row.artifact == "art1"
        assert row.bytes_downloaded == 123
        assert row.rows_parsed == 1
    finally:
        with database.session() as s:
            for tbl in (
                "etl_file_metrics",
                "data_quality_checks",
                "ingestion_runs",
                "security_attribute_history",
                "securities",
            ):
                s.execute(text(f"DELETE FROM {tbl} WHERE source=:x"), {"x": src_name})


def test_cross_source_reconciliation_flags_coupon_mismatch(database: Database) -> None:
    src_a, src_b, isin = "reconA", "reconB", "INUNIV000009"

    def _mk(source: str, coupon: float) -> SecurityRecord:
        return SecurityRecord(
            isin=isin,
            instrument_type=InstrumentType.CORP,
            source=source,
            coupon=coupon,
            maturity_date=dt.date(2030, 1, 1),
        )

    try:
        UniversePipeline(database, source=FakeUniverseSource([_mk(src_a, 7.0)], name=src_a)).run(
            DAY1
        )
        # src_b reports a different coupon for the same ISIN -> mismatch
        UniversePipeline(database, source=FakeUniverseSource([_mk(src_b, 8.5)], name=src_b)).run(
            DAY1
        )
        with database.session() as s:
            observed = s.execute(
                text(
                    "SELECT observed FROM data_quality_checks WHERE source=:src "
                    "AND check_name='cross_source_coupon_mismatch' ORDER BY id DESC LIMIT 1"
                ),
                {"src": src_b},
            ).scalar_one()
        assert observed == 1.0
    finally:
        with database.session() as s:
            for src in (src_a, src_b):
                s.execute(delete(Security).where(Security.source == src))
                s.execute(delete(IngestionRun).where(IngestionRun.source == src))
                s.execute(text("DELETE FROM data_quality_checks WHERE source=:s"), {"s": src})
