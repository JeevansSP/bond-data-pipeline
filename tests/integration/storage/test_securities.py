"""Integration tests for SecurityRepository.insert_missing (needs Postgres)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import delete, select

from bonds.models import InstrumentType, SecurityRecord
from bonds.storage import Database, Security
from bonds.storage.repositories import SecurityRepository

pytestmark = pytest.mark.integration

ISIN = "INSECTEST001"  # sentinel -> non-destructive
DAY = dt.date(2026, 7, 18)


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database()
    database.create_all()

    def _clean() -> None:
        with database.session() as s:
            s.execute(delete(Security).where(Security.isin == ISIN))

    _clean()
    yield database
    _clean()


def _sec(source: str, itype: InstrumentType, coupon: float | None) -> SecurityRecord:
    return SecurityRecord(isin=ISIN, instrument_type=itype, source=source, coupon=coupon)


def test_upsert_out_of_order_keeps_true_seen_window(db: Database) -> None:
    # Insert as of a later date, then re-ingest an OLDER snapshot; first_seen must move to the
    # earliest date and last_seen stay at the latest (GREATEST/LEAST), never regressing.
    rec = _sec("fbil", InstrumentType.GSEC, 6.94)
    with db.session() as s:
        SecurityRepository(s).upsert_many([rec], seen_on=dt.date(2026, 7, 18))
    with db.session() as s:
        SecurityRepository(s).upsert_many([rec], seen_on=dt.date(2026, 7, 10))  # older, second
    with db.session() as s:
        row = s.execute(select(Security).where(Security.isin == ISIN)).scalar_one()
        assert row.first_seen == dt.date(2026, 7, 10)  # earliest
        assert row.last_seen == dt.date(2026, 7, 18)  # latest, did not regress


def test_enrich_missing_fills_nulls_without_overwriting(db: Database) -> None:
    # Seed a sparse row (coupon null, issuer set), then enrich: null coupon fills, set issuer stays.
    with db.session() as s:
        SecurityRepository(s).upsert_many(
            [
                SecurityRecord(
                    isin=ISIN,
                    instrument_type=InstrumentType.CORP,
                    source="cdsl",
                    issuer="Original Issuer",
                )
            ],
            seen_on=DAY,
        )
    enrichment = SecurityRecord(
        isin=ISIN,
        instrument_type=InstrumentType.CORP,
        source="bondcentral",
        coupon=8.5,
        issuer="BondCentral Issuer",  # must NOT overwrite the existing issuer
        maturity_date=dt.date(2030, 6, 1),
    )
    with db.session() as s:
        SecurityRepository(s).enrich_missing([enrichment])
    with db.session() as s:
        row = s.execute(select(Security).where(Security.isin == ISIN)).scalar_one()
        assert row.coupon == pytest.approx(8.5)  # null -> filled
        assert row.maturity_date == dt.date(2030, 6, 1)  # null -> filled
        assert row.issuer == "Original Issuer"  # already set -> preserved


def test_upsert_null_reference_fields_do_not_regress_enriched_values(db: Database) -> None:
    # An enriched row (coupon/maturity filled) must survive a later universe snapshot whose
    # payload carries nulls for those fields — otherwise nightly upserts undo enrichment daily.
    with db.session() as s:
        SecurityRepository(s).upsert_many(
            [
                SecurityRecord(
                    isin=ISIN,
                    instrument_type=InstrumentType.CORP,
                    source="bondcentral",
                    coupon=8.5,
                    maturity_date=dt.date(2030, 6, 1),
                    issuer="Issuer Ltd",
                )
            ],
            seen_on=DAY,
        )
    with db.session() as s:
        SecurityRepository(s).upsert_many(
            [_sec("bondcentral", InstrumentType.CORP, None)],  # null coupon in today's listing
            seen_on=DAY + dt.timedelta(days=1),
        )
    with db.session() as s:
        row = s.execute(select(Security).where(Security.isin == ISIN)).scalar_one()
        assert row.coupon == pytest.approx(8.5)  # preserved, not re-nulled
        assert row.maturity_date == dt.date(2030, 6, 1)
        assert row.issuer == "Issuer Ltd"
        assert row.last_seen == DAY + dt.timedelta(days=1)  # still advanced


def test_insert_missing_inserts_then_does_not_overwrite(db: Database) -> None:
    # First insert (as if from FBIL) writes the row; the return value is the TRUE insert count
    # (cursor.rowcount is -1 for multi-row VALUES, so this must come from RETURNING).
    with db.session() as s:
        assert (
            SecurityRepository(s).insert_missing(
                [_sec("fbil", InstrumentType.GSEC, 6.94)], seen_on=DAY
            )
            == 1
        )
    # A later CCIL-derived insert for the SAME ISIN must NOT overwrite the authoritative row.
    with db.session() as s:
        assert (
            SecurityRepository(s).insert_missing(
                [_sec("ccil", InstrumentType.TBILL, 0.0)], seen_on=DAY
            )
            == 0
        )
    with db.session() as s:
        row = s.execute(select(Security).where(Security.isin == ISIN)).scalar_one()
        assert row.source == "fbil"  # unchanged
        assert row.instrument_type == "GSEC"
        assert row.coupon == pytest.approx(6.94)
