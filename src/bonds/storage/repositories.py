"""Repositories encapsulating all read/write access to the schema.

Upserts use Postgres ``INSERT ... ON CONFLICT`` so pipelines are idempotent: re-running a
date simply refreshes its rows rather than duplicating or erroring.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence

from sqlalchemy import CursorResult, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from bonds.logging import get_logger
from bonds.models import (
    PublicIssueRecord,
    RbiAuctionRecord,
    SecurityRecord,
    SovereignValuation,
    TradeRecord,
)
from bonds.quality.metrics import FileMetric
from bonds.storage.schema import (
    DataQualityCheck,
    EtlFileMetric,
    IngestionRun,
    PublicIssue,
    RbiAuction,
    Security,
    SecurityAttributeHistory,
    Trade,
    Valuation,
)

logger = get_logger(__name__)

# Postgres caps a statement at 65535 bind parameters; chunk multi-row inserts well under that
# (widest row here is ~10 columns, so 1000 rows -> ~10k params).
_CHUNK_ROWS = 1000


def _chunks[T](items: Sequence[T], size: int = _CHUNK_ROWS) -> Iterator[Sequence[T]]:
    """Yield ``items`` in slices of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _apply_scd2(
    current: SecurityAttributeHistory | None,
    isin: str,
    attribute: str,
    value: str | None,
    effective: dt.date,
    source: str,
) -> bool:
    """Decide the SCD-2 transition, mutating ``current`` as needed.

    Returns ``True`` if a change should be recorded. The caller inserts a new open row when
    ``current is None`` or ``effective > current.valid_from`` (a genuine forward change).

    - unchanged value               -> ``False`` (no-op)
    - ``effective < valid_from``     -> ``False`` (out-of-order backfill; attribute history must be
      ingested chronologically — skip rather than overwrite the newer value or collide)
    - ``effective == valid_from``    -> update value+source in place, ``True`` (same-day correction)
    - ``effective > valid_from``     -> close the open row, ``True`` (caller opens a new one)
    """
    if current is None:
        return True
    if current.value == value:
        return False
    if effective < current.valid_from:
        logger.warning(
            "scd2.out_of_order_skipped",
            isin=isin,
            attribute=attribute,
            effective=effective.isoformat(),
            current_from=current.valid_from.isoformat(),
        )
        return False
    if effective == current.valid_from:
        current.value = value
        current.source = source
        return True
    current.valid_to = effective - dt.timedelta(days=1)
    return True


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

    def insert_missing(self, records: list[SecurityRecord], *, seen_on: dt.date) -> int:
        """Insert only securities whose ISIN is not already present (``ON CONFLICT DO NOTHING``).

        Used to fill the master with instruments seen only in trade data (T-Bills, STRIPS, SGBs,
        matured G-Secs/SDLs) without overwriting the richer rows an authoritative universe source
        (FBIL/BondCentral) already wrote. Returns the number of rows actually inserted.
        """
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
        rows = list({r["isin"]: r for r in rows}.values())
        inserted = 0
        for chunk in _chunks(rows):
            stmt = (
                pg_insert(Security)
                .values(list(chunk))
                .on_conflict_do_nothing(index_elements=["isin"])
            )
            result = self._session.execute(stmt)
            if isinstance(result, CursorResult):
                inserted += max(result.rowcount, 0)  # rowcount is -1 when the driver can't report
        return inserted

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
        # .first() (not scalar_one_or_none) so a legacy multi-open-row state can't crash the run.
        current = (
            self._session.execute(
                select(SecurityAttributeHistory)
                .where(
                    SecurityAttributeHistory.isin == isin,
                    SecurityAttributeHistory.attribute == attribute,
                    SecurityAttributeHistory.valid_to.is_(None),
                )
                .order_by(SecurityAttributeHistory.valid_from.desc())
            )
            .scalars()
            .first()
        )
        if not _apply_scd2(current, isin, attribute, value, effective, source):
            return False
        if current is None or effective > current.valid_from:
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
            if not _apply_scd2(existing, isin, attribute, value, effective, source):
                continue
            if existing is None or effective > existing.valid_from:
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
    """Idempotent ingestion audit records (one row per source+dataset+run_date)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        source: str,
        dataset: str,
        run_date: dt.date,
        status: str,
        rows: int,
        started_at: dt.datetime,
        message: str | None = None,
    ) -> None:
        """Upsert the terminal audit record for a run (re-running a day overwrites it)."""
        stmt = pg_insert(IngestionRun).values(
            source=source,
            dataset=dataset,
            run_date=run_date,
            status=status,
            rows_ingested=rows,
            message=message,
            started_at=started_at,
            finished_at=dt.datetime.now(dt.UTC),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "dataset", "run_date"],
            set_={
                "status": stmt.excluded.status,
                "rows_ingested": stmt.excluded.rows_ingested,
                "message": stmt.excluded.message,
                "started_at": stmt.excluded.started_at,
                "finished_at": stmt.excluded.finished_at,
            },
        )
        self._session.execute(stmt)

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

    def last_processed_date(self, source: str) -> dt.date | None:
        """Most recent ``run_date`` for ``source`` that reached a terminal success or skip.

        This is the anchor for gap-fill catch-up: skips (holidays/no-data days) count as processed
        so they are not retried forever, while failed days are excluded so they get re-attempted.
        Returns ``None`` if the source has never been ingested.
        """
        return self._session.execute(
            select(func.max(IngestionRun.run_date)).where(
                IngestionRun.source == source,
                IngestionRun.status.in_(("success", "skipped")),
            )
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


class TradeRepository:
    """Persist secondary-market trade summaries (idempotent per isin+date+source+segment)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, trades: list[TradeRecord]) -> int:
        """Insert or refresh a batch of trades. Returns rows written."""
        if not trades:
            return 0
        rows = [
            {
                "isin": t.isin,
                "trade_date": t.trade_date,
                "source": t.source,
                "segment": t.segment,
                "descriptor": t.descriptor,
                "ltp": t.ltp,
                "lty": t.lty,
                "no_of_trades": t.no_of_trades,
                "trade_value": t.trade_value,
                "wap": t.wap,
                "way": t.way,
            }
            for t in trades
        ]
        rows = list(
            {(r["isin"], r["trade_date"], r["source"], r["segment"]): r for r in rows}.values()
        )
        for chunk in _chunks(rows):
            stmt = pg_insert(Trade).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=["isin", "trade_date", "source", "segment"],
                set_={
                    "descriptor": stmt.excluded.descriptor,
                    "ltp": stmt.excluded.ltp,
                    "lty": stmt.excluded.lty,
                    "no_of_trades": stmt.excluded.no_of_trades,
                    "trade_value": stmt.excluded.trade_value,
                    "wap": stmt.excluded.wap,
                    "way": stmt.excluded.way,
                },
            )
            self._session.execute(stmt)
        return len(rows)


class RbiAuctionRepository:
    """Persist RBI auction calendar records (idempotent per prid + source)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, auctions: list[RbiAuctionRecord], *, seen_on: dt.date) -> int:
        """Insert or refresh auctions, setting ``first_seen`` on insert; advancing ``last_seen``."""
        if not auctions:
            return 0
        rows = [
            {
                "prid": a.prid,
                "title": a.title,
                "auction_type": a.auction_type,
                "auction_date": a.auction_date,
                "detail_url": a.detail_url,
                "pdf_url": a.pdf_url,
                "source": a.source,
                "first_seen": seen_on,
                "last_seen": seen_on,
            }
            for a in auctions
        ]
        rows = list({(r["prid"], r["source"]): r for r in rows}.values())
        for chunk in _chunks(rows):
            stmt = pg_insert(RbiAuction).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=["prid", "source"],
                set_={
                    "title": stmt.excluded.title,
                    "auction_type": stmt.excluded.auction_type,
                    "auction_date": stmt.excluded.auction_date,
                    "detail_url": stmt.excluded.detail_url,
                    "pdf_url": stmt.excluded.pdf_url,
                    "last_seen": stmt.excluded.last_seen,
                },
            )
            self._session.execute(stmt)
        return len(rows)


class EtlMetricsRepository:
    """Persist per-artifact ETL funnel metrics (idempotent per source+dataset+run_date+artifact)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self, *, source: str, dataset: str, run_date: dt.date, metrics: list[FileMetric]
    ) -> None:
        """Upsert the extract/transform funnel metrics for each artifact of a run."""
        if not metrics:
            return
        rows = [
            {
                "source": source,
                "dataset": dataset,
                "run_date": run_date,
                "artifact": m.artifact,
                "bytes_downloaded": m.bytes_downloaded,
                "rows_extracted": m.rows_extracted,
                "rows_parsed": m.rows_parsed,
                "rows_dropped": m.rows_dropped,
            }
            for m in metrics
        ]
        rows = list({r["artifact"]: r for r in rows}.values())
        stmt = pg_insert(EtlFileMetric).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "dataset", "run_date", "artifact"],
            set_={
                "bytes_downloaded": stmt.excluded.bytes_downloaded,
                "rows_extracted": stmt.excluded.rows_extracted,
                "rows_parsed": stmt.excluded.rows_parsed,
                "rows_dropped": stmt.excluded.rows_dropped,
            },
        )
        self._session.execute(stmt)


class DataQualityRepository:
    """Persist data-quality check results (idempotent per dataset+run_date+check_name)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, rows: list[dict[str, object]]) -> None:
        """Upsert check results so a same-day re-run overwrites rather than duplicates."""
        if not rows:
            return
        # Guard the conflict key so a duplicate check_name in one batch can't 'affect a row twice'.
        rows = list({(r["dataset"], r["run_date"], r["check_name"]): r for r in rows}.values())
        stmt = pg_insert(DataQualityCheck).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["dataset", "run_date", "check_name"],
            set_={
                "source": stmt.excluded.source,
                "level": stmt.excluded.level,
                "passed": stmt.excluded.passed,
                "observed": stmt.excluded.observed,
                "detail": stmt.excluded.detail,
            },
        )
        self._session.execute(stmt)
