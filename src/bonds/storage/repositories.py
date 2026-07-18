"""Repositories encapsulating all read/write access to the schema.

Upserts use Postgres ``INSERT ... ON CONFLICT`` so pipelines are idempotent: re-running a
date simply refreshes its rows rather than duplicating or erroring.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from bonds.models import PublicIssueRecord, SecurityRecord, SovereignValuation
from bonds.storage.schema import (
    DataQualityCheck,
    IngestionRun,
    PublicIssue,
    Security,
    SecurityAttributeHistory,
    Valuation,
)

# Postgres caps a statement at 65535 bind parameters; chunk multi-row inserts well under that
# (widest row here is ~10 columns, so 1000 rows -> ~10k params).
_CHUNK_ROWS = 1000


def _chunks[T](items: Sequence[T], size: int = _CHUNK_ROWS) -> Iterator[Sequence[T]]:
    """Yield ``items`` in slices of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


class ValuationRepository:
    """Persist daily per-ISIN valuations (idempotent per ``(isin, quote_date, source)``)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, valuations: list[SovereignValuation]) -> int:
        """Insert or refresh a batch of valuations. Returns the number of rows written."""
        if not valuations:
            return 0
        rows = [
            {
                "isin": v.isin,
                "quote_date": v.quote_date,
                "source": v.source,
                "instrument_type": v.instrument_type.value,
                "description": v.description,
                "coupon": v.coupon,
                "maturity_date": v.maturity_date,
                "price": v.price,
                "ytm": v.ytm,
            }
            for v in valuations
        ]
        # A single INSERT ... ON CONFLICT cannot touch the same key twice; dedupe (last wins).
        rows = list({(r["isin"], r["quote_date"], r["source"]): r for r in rows}.values())
        for chunk in _chunks(rows):
            stmt = pg_insert(Valuation).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=["isin", "quote_date", "source"],
                set_={
                    "instrument_type": stmt.excluded.instrument_type,
                    "description": stmt.excluded.description,
                    "coupon": stmt.excluded.coupon,
                    "maturity_date": stmt.excluded.maturity_date,
                    "price": stmt.excluded.price,
                    "ytm": stmt.excluded.ytm,
                },
            )
            self._session.execute(stmt)
        return len(rows)


class SecurityRepository:
    """Upsert universe securities and maintain SCD-2 attribute history."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, records: list[SecurityRecord], *, seen_on: dt.date) -> int:
        """Upsert securities, setting ``first_seen`` on insert and advancing ``last_seen``."""
        if not records:
            return 0
        rows = [
            {
                "isin": r.isin,
                "instrument_type": r.instrument_type.value,
                "description": r.description,
                "issuer": r.issuer,
                "coupon": r.coupon,
                "interest_type": r.interest_type,
                "maturity_date": r.maturity_date,
                "face_value": r.face_value,
                "source": r.source,
                "first_seen": seen_on,
                "last_seen": seen_on,
            }
            for r in records
        ]
        # A single INSERT ... ON CONFLICT cannot touch the same ISIN twice; dedupe (last wins).
        rows = list({r["isin"]: r for r in rows}.values())
        for chunk in _chunks(rows):
            stmt = pg_insert(Security).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=["isin"],
                set_={
                    "instrument_type": stmt.excluded.instrument_type,
                    "description": stmt.excluded.description,
                    "issuer": stmt.excluded.issuer,
                    "coupon": stmt.excluded.coupon,
                    "interest_type": stmt.excluded.interest_type,
                    "maturity_date": stmt.excluded.maturity_date,
                    "face_value": stmt.excluded.face_value,
                    "source": stmt.excluded.source,
                    # first_seen is preserved (only set on insert); last_seen advances.
                    "last_seen": stmt.excluded.last_seen,
                },
            )
            self._session.execute(stmt)
        return len(rows)

    def load_reference(
        self, isins: list[str]
    ) -> dict[str, tuple[float | None, dt.date | None, str]]:
        """Return ``{isin: (coupon, maturity_date, source)}`` for the currently-stored rows."""
        if not isins:
            return {}
        result: dict[str, tuple[float | None, dt.date | None, str]] = {}
        for chunk in _chunks(isins):
            rows = self._session.execute(
                select(
                    Security.isin, Security.coupon, Security.maturity_date, Security.source
                ).where(Security.isin.in_(list(chunk)))
            ).all()
            for isin, coupon, maturity, source in rows:
                result[isin] = (coupon, maturity, source)
        return result

    def record_attribute(
        self, isin: str, attribute: str, value: str | None, *, effective: dt.date, source: str
    ) -> bool:
        """Append an SCD-2 row iff ``value`` differs from the current one.

        Returns:
            ``True`` if a change was recorded, ``False`` if the value was unchanged.
        """
        current = self._session.execute(
            select(SecurityAttributeHistory)
            .where(
                SecurityAttributeHistory.isin == isin,
                SecurityAttributeHistory.attribute == attribute,
                SecurityAttributeHistory.valid_to.is_(None),
            )
            .order_by(SecurityAttributeHistory.valid_from.desc())
        ).scalar_one_or_none()

        if current is not None and current.value == value:
            return False
        if current is not None:
            current.valid_to = effective - dt.timedelta(days=1)

        self._session.add(
            SecurityAttributeHistory(
                isin=isin,
                attribute=attribute,
                value=value,
                valid_from=effective,
                valid_to=None,
                source=source,
            )
        )
        return True

    def record_attribute_bulk(
        self, attribute: str, values: dict[str, str | None], *, effective: dt.date, source: str
    ) -> int:
        """SCD-2 many ISINs for one ``attribute`` in a single pass.

        Loads all currently-open rows for ``attribute`` once (one query), diffs in memory, and
        writes only genuine changes. Far cheaper than per-ISIN :meth:`record_attribute` when
        ingesting a whole universe.

        Returns:
            The number of changed values recorded.
        """
        if not values:
            return 0
        open_rows = (
            self._session.execute(
                select(SecurityAttributeHistory).where(
                    SecurityAttributeHistory.attribute == attribute,
                    SecurityAttributeHistory.valid_to.is_(None),
                )
            )
            .scalars()
            .all()
        )
        current = {row.isin: row for row in open_rows}

        changes = 0
        for isin, value in values.items():
            existing = current.get(isin)
            if existing is not None and existing.value == value:
                continue
            if existing is not None:
                existing.valid_to = effective - dt.timedelta(days=1)
            self._session.add(
                SecurityAttributeHistory(
                    isin=isin,
                    attribute=attribute,
                    value=value,
                    valid_from=effective,
                    valid_to=None,
                    source=source,
                )
            )
            changes += 1
        return changes


class IngestionRunRepository:
    """Create and finalise ingestion audit records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def start(self, *, source: str, dataset: str, run_date: dt.date) -> IngestionRun:
        """Open an ingestion run in ``running`` state and return it."""
        run = IngestionRun(
            source=source,
            dataset=dataset,
            run_date=run_date,
            status="running",
            rows_ingested=0,
            started_at=dt.datetime.now(dt.UTC),
        )
        self._session.add(run)
        self._session.flush()
        return run

    def finish(
        self, run: IngestionRun, *, status: str, rows: int = 0, message: str | None = None
    ) -> None:
        """Close an ingestion run with a terminal ``status`` and row count."""
        run.status = status
        run.rows_ingested = rows
        run.message = message
        run.finished_at = dt.datetime.now(dt.UTC)
        self._session.add(run)

    def previous_row_count(self, dataset: str, *, before: dt.date) -> int | None:
        """Rows ingested by the most recent successful run of ``dataset`` before ``before``."""
        return self._session.execute(
            select(IngestionRun.rows_ingested)
            .where(
                IngestionRun.dataset == dataset,
                IngestionRun.status == "success",
                IngestionRun.run_date < before,
            )
            .order_by(IngestionRun.run_date.desc())
            .limit(1)
        ).scalar_one_or_none()


class PublicIssueRepository:
    """Persist SEBI public-issue records (idempotent per company + open date + source)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, issues: list[PublicIssueRecord]) -> int:
        """Insert or refresh a batch of public issues. Returns rows written."""
        if not issues:
            return 0
        rows = [
            {
                "company": i.company,
                "issue_open": i.issue_open,
                "source": i.source,
                "issue_close": i.issue_close,
                "base_size_cr": i.base_size_cr,
                "final_size_cr": i.final_size_cr,
                "financial_year": i.financial_year,
            }
            for i in issues
        ]
        rows = list({(r["company"], r["issue_open"], r["source"]): r for r in rows}.values())
        for chunk in _chunks(rows):
            stmt = pg_insert(PublicIssue).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=["company", "issue_open", "source"],
                set_={
                    "issue_close": stmt.excluded.issue_close,
                    "base_size_cr": stmt.excluded.base_size_cr,
                    "final_size_cr": stmt.excluded.final_size_cr,
                    "financial_year": stmt.excluded.financial_year,
                },
            )
            self._session.execute(stmt)
        return len(rows)


class DataQualityRepository:
    """Persist data-quality check results."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        checks: list[DataQualityCheck],
    ) -> None:
        """Persist a batch of already-constructed :class:`DataQualityCheck` rows."""
        self._session.add_all(checks)
