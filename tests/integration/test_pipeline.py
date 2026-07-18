"""Integration tests for the storage layer + sovereign valuation pipeline.

Requires a live Postgres (``docker compose up -d postgres``). Run with::

    uv run pytest -m integration

Uses sentinel ISINs (``INTEST______``) and source ``faketest`` so it is non-destructive to
any real ingested data, cleaning up only its own rows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select

from bonds.models import InstrumentType, SovereignValuation
from bonds.pipelines import RunStatus, SovereignValuationPipeline
from bonds.sources.base import DataUnavailable
from bonds.storage import Database, IngestionRun, Security, SecurityAttributeHistory, Valuation
from bonds.storage.repositories import SecurityRepository

pytestmark = pytest.mark.integration

SOURCE = "faketest"
ISIN_A = "INTEST000001"
ISIN_B = "INTEST000002"
DATE = dt.date(2026, 7, 10)


class FakeFetcher:
    """A :class:`ValuationFetcher` returning canned rows (no network)."""

    name = SOURCE

    def __init__(self, *, unavailable: bool = False) -> None:
        self._unavailable = unavailable

    def fetch_valuations(self, product: str, date: dt.date) -> list[SovereignValuation]:
        if self._unavailable:
            raise DataUnavailable(f"no data for {date}")
        return [
            SovereignValuation(
                isin=ISIN_A,
                quote_date=date,
                instrument_type=InstrumentType.GSEC,
                source=SOURCE,
                coupon=6.97,
                price=100.24,
                ytm=5.26,
            ),
            SovereignValuation(
                isin=ISIN_B,
                quote_date=date,
                instrument_type=InstrumentType.GSEC,
                source=SOURCE,
                coupon=7.10,
                price=99.80,
                ytm=7.20,
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
            s.execute(delete(Valuation).where(Valuation.source == SOURCE))
            s.execute(delete(Security).where(Security.source == SOURCE))
            s.execute(
                delete(SecurityAttributeHistory).where(SecurityAttributeHistory.source == SOURCE)
            )
            s.execute(delete(IngestionRun).where(IngestionRun.source == SOURCE))

    wipe()
    yield
    wipe()


def test_run_date_persists_valuations_and_securities(database: Database) -> None:
    pipeline = SovereignValuationPipeline(database, source=FakeFetcher(), products=["gsec"])
    results = pipeline.run_date(DATE)

    assert [r.status for r in results] == [RunStatus.SUCCESS]
    assert results[0].rows == 2

    with database.session() as s:
        vals = s.execute(select(Valuation).where(Valuation.source == SOURCE)).scalars().all()
        secs = s.execute(select(Security).where(Security.source == SOURCE)).scalars().all()
        runs = s.execute(select(IngestionRun).where(IngestionRun.source == SOURCE)).scalars().all()
    assert {v.isin for v in vals} == {ISIN_A, ISIN_B}
    assert {sec.isin for sec in secs} == {ISIN_A, ISIN_B}
    assert runs[0].status == "success" and runs[0].rows_ingested == 2


def test_run_date_is_idempotent(database: Database) -> None:
    pipeline = SovereignValuationPipeline(database, source=FakeFetcher(), products=["gsec"])
    pipeline.run_date(DATE)
    pipeline.run_date(DATE)  # re-run same date
    with database.session() as s:
        count = len(s.execute(select(Valuation).where(Valuation.source == SOURCE)).scalars().all())
    assert count == 2  # upsert, not duplicate


def test_unavailable_day_is_skipped(database: Database) -> None:
    pipeline = SovereignValuationPipeline(
        database, source=FakeFetcher(unavailable=True), products=["gsec"]
    )
    results = pipeline.run_date(DATE)
    assert results[0].status == RunStatus.SKIPPED
    with database.session() as s:
        vals = s.execute(select(Valuation).where(Valuation.source == SOURCE)).scalars().all()
    assert vals == []


def test_scd2_attribute_history_records_only_changes(database: Database) -> None:
    with database.session() as s:
        repo = SecurityRepository(s)
        assert repo.record_attribute(ISIN_A, "rating", "AAA", effective=DATE, source=SOURCE)
        # same value -> no new row
        assert not repo.record_attribute(
            ISIN_A, "rating", "AAA", effective=DATE + dt.timedelta(days=1), source=SOURCE
        )
        # changed value -> new row, previous closed
        assert repo.record_attribute(
            ISIN_A, "rating", "AA+", effective=DATE + dt.timedelta(days=5), source=SOURCE
        )

    with database.session() as s:
        history = (
            s.execute(
                select(SecurityAttributeHistory)
                .where(SecurityAttributeHistory.isin == ISIN_A)
                .order_by(SecurityAttributeHistory.valid_from)
            )
            .scalars()
            .all()
        )
    assert [(h.value, h.valid_to) for h in history] == [
        ("AAA", DATE + dt.timedelta(days=4)),
        ("AA+", None),
    ]
