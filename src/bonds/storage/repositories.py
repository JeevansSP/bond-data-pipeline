"""Repositories encapsulating all read/write access to the schema.

Upserts use Postgres ``INSERT ... ON CONFLICT`` so pipelines are idempotent: re-running a
date simply refreshes its rows rather than duplicating or erroring.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import CursorResult, and_, case, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from bonds.logging import get_logger
from bonds.models import (
    CorporateTradeRecord,
    PublicIssueRecord,
    RbiAuctionRecord,
    RbiAuctionResultRecord,
    SecurityRecord,
    SovereignValuation,
    TradeRecord,
    YieldCurvePoint,
)

if TYPE_CHECKING:
    # Annotation-only: a runtime import would create a cycle (bonds.quality's __init__ imports
    # the inspector, which imports this module) that bites any caller importing bonds.storage
    # before bonds.quality.
    from bonds.quality.metrics import FileMetric
from bonds.storage.schema import (
    CorporateTrade,
    DataQualityCheck,
    EtlFileMetric,
    IngestionRun,
    PublicIssue,
    RbiAuction,
    RbiAuctionResult,
    Security,
    SecurityAttributeHistory,
    Trade,
    Valuation,
    YieldCurve,
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

    - no history and no value       -> ``False`` (day-1 must not flood with "unrated" rows)
    - unchanged value               -> ``False`` (no-op)
    - ``effective < valid_from``     -> ``False`` (out-of-order backfill; attribute history must be
      ingested chronologically — skip rather than overwrite the newer value or collide)
    - value withdrawn (``None``) by a *different* source -> ``False`` (a source that simply doesn't
      carry this attribute — e.g. CDSL never sends ``credit_rating`` — must not close a value
      another source set; only the source that set it may withdraw it)
    - ``effective == valid_from``    -> update value+source in place, ``True`` (same-day correction)
    - ``effective > valid_from``     -> close the open row, ``True`` (caller opens a new one)
    """
    if current is None:
        return value is not None
    if current.value == value:
        return False
    if effective < current.valid_from:
        # Debug, not warning: an out-of-order backfill (e.g. an old half-yearly CDSL snapshot
        # ingested after newer BondCentral history) hits this once per ISIN — tens of thousands
        # of lines in one run at warning level.
        logger.debug(
            "scd2.out_of_order_skipped",
            isin=isin,
            attribute=attribute,
            effective=effective.isoformat(),
            current_from=current.valid_from.isoformat(),
        )
        return False
    if value is None and current.source != source:
        return False
    if effective == current.valid_from:
        current.value = value
        current.source = source
        return True
    current.valid_to = effective - dt.timedelta(days=1)
    return True


class ValuationRepository:
    """Persist daily per-ISIN valuations, append-only per ``(isin, quote_date, source)``.

    Re-running a date with identical values is a no-op. A *changed* value (source
    restatement) closes the current row (``superseded_at``) and inserts a new one, so
    prior published values remain queryable for audit.
    """

    # Fields whose change constitutes a restatement (vs. cosmetic re-parse noise).
    _VERSIONED_FIELDS = (
        "instrument_type",
        "description",
        "coupon",
        "maturity_date",
        "price",
        "ytm",
    )

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, valuations: list[SovereignValuation]) -> int:
        """Write a batch of valuations. Returns the number of new row versions inserted."""
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
        # The same key may not be touched twice in one batch; dedupe (last wins).
        rows = list({(r["isin"], r["quote_date"], r["source"]): r for r in rows}.values())
        written = 0
        for chunk in _chunks(rows):
            written += self._write_chunk(chunk)
        return written

    def _write_chunk(self, chunk: Sequence[dict[str, str | float | dt.date | None]]) -> int:
        by_key = {(r["isin"], r["quote_date"], r["source"]): r for r in chunk}
        current = self._session.execute(
            select(Valuation).where(
                tuple_(Valuation.isin, Valuation.quote_date, Valuation.source).in_(
                    list(by_key.keys())
                ),
                Valuation.superseded_at.is_(None),
            )
        ).scalars()
        superseded_ids: list[int] = []
        for row in current:
            incoming = by_key[(row.isin, row.quote_date, row.source)]
            if all(getattr(row, f) == incoming[f] for f in self._VERSIONED_FIELDS):
                del by_key[(row.isin, row.quote_date, row.source)]  # unchanged -> no-op
            else:
                superseded_ids.append(row.id)  # restated -> close current, insert fresh
        if superseded_ids:
            self._session.execute(
                update(Valuation)
                .where(Valuation.id.in_(superseded_ids))
                .values(superseded_at=func.now())
            )
            logger.info("valuations.superseded", rows=len(superseded_ids))
        remaining = list(by_key.values())
        if remaining:
            self._session.execute(pg_insert(Valuation).values(remaining))
        return len(remaining)


class YieldCurveRepository:
    """Persist yield-curve points, append-only per ``(curve, quote_date, tenor_years, source)``.

    Same bitemporal discipline as :class:`ValuationRepository`: identical re-runs are
    no-ops; changed values close the current row and insert a fresh version.
    """

    _VERSIONED_FIELDS = ("ytm_semi_annual", "ytm_annualized")

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, points: list[YieldCurvePoint]) -> int:
        """Write a batch of curve points. Returns the number of new row versions inserted."""
        if not points:
            return 0
        rows = [
            {
                "curve": p.curve,
                "quote_date": p.quote_date,
                "tenor_years": p.tenor_years,
                "source": p.source,
                "ytm_semi_annual": p.ytm_semi_annual,
                "ytm_annualized": p.ytm_annualized,
            }
            for p in points
        ]
        rows = list(
            {(r["curve"], r["quote_date"], r["tenor_years"], r["source"]): r for r in rows}.values()
        )
        written = 0
        for chunk in _chunks(rows):
            written += self._write_chunk(chunk)
        return written

    def _write_chunk(self, chunk: Sequence[dict[str, str | float | dt.date | None]]) -> int:
        by_key = {(r["curve"], r["quote_date"], r["tenor_years"], r["source"]): r for r in chunk}
        current = self._session.execute(
            select(YieldCurve).where(
                tuple_(
                    YieldCurve.curve,
                    YieldCurve.quote_date,
                    YieldCurve.tenor_years,
                    YieldCurve.source,
                ).in_(list(by_key.keys())),
                YieldCurve.superseded_at.is_(None),
            )
        ).scalars()
        superseded_ids: list[int] = []
        for row in current:
            key = (row.curve, row.quote_date, row.tenor_years, row.source)
            incoming = by_key[key]
            if all(getattr(row, f) == incoming[f] for f in self._VERSIONED_FIELDS):
                del by_key[key]  # unchanged -> no-op
            else:
                superseded_ids.append(row.id)  # restated -> close current, insert fresh
        if superseded_ids:
            self._session.execute(
                update(YieldCurve)
                .where(YieldCurve.id.in_(superseded_ids))
                .values(superseded_at=func.now())
            )
            logger.info("yield_curves.superseded", rows=len(superseded_ids))
        remaining = list(by_key.values())
        if remaining:
            self._session.execute(pg_insert(YieldCurve).values(remaining))
        return len(remaining)


class CorporateTradeRepository:
    """Persist trade-level corporate trades.

    There is no natural transaction key across venues (BSE publishes no trade id), so
    idempotency is delete-and-replace over a ``(source, trade-date window)``: reloading a
    window converges to the file's contents rather than duplicating.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def replace_window(
        self, source: str, start: dt.date, end: dt.date, trades: list[CorporateTradeRecord]
    ) -> int:
        """Replace ``source``'s rows in ``[start, end]`` with ``trades``; returns rows written."""
        result = self._session.execute(
            delete(CorporateTrade).where(
                CorporateTrade.source == source,
                CorporateTrade.trade_date >= start,
                CorporateTrade.trade_date <= end,
            )
        )
        deleted = result.rowcount if isinstance(result, CursorResult) else 0
        if deleted:
            logger.info(
                "corporate_trades.replaced",
                source=source,
                start=start.isoformat(),
                end=end.isoformat(),
                deleted=deleted,
            )
        rows = [
            {
                "isin": t.isin,
                "trade_date": t.trade_date,
                "source": t.source,
                "trade_time": t.trade_time,
                "listed": t.listed,
                "deal_type": t.deal_type,
                "seller_deal_type": t.seller_deal_type,
                "buyer_deal_type": t.buyer_deal_type,
                "issuer": t.issuer,
                "description": t.description,
                "coupon": t.coupon,
                "price": t.price,
                "trade_yield": t.trade_yield,
                "yield_type": t.yield_type,
                "outside_yield_range": t.outside_yield_range,
                "put_call_date": t.put_call_date,
                "trade_value_lakh": t.trade_value_lakh,
                "settlement_date": t.settlement_date,
                "settlement_status": t.settlement_status,
                "venue": t.venue,
                "remarks": t.remarks,
            }
            for t in trades
            if start <= t.trade_date <= end  # never write outside the window being replaced
        ]
        for chunk in _chunks(rows):
            self._session.execute(pg_insert(CorporateTrade).values(list(chunk)))
        return len(rows)


def _security_row(r: SecurityRecord, *, seen_on: dt.date) -> dict[str, object]:
    """The ``securities`` insert row for one record (shared by upsert/insert paths)."""
    return {
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


class SecurityRepository:
    """Upsert universe securities and maintain SCD-2 attribute history."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, records: list[SecurityRecord], *, seen_on: dt.date) -> int:
        """Upsert securities, setting ``first_seen`` on insert and advancing ``last_seen``.

        Reference fields (description/issuer/coupon/…) are refreshed but never regressed to
        NULL: a source snapshot that omits a value (e.g. a BondCentral listing row with a null
        coupon) must not wipe what another pass — notably enrichment — already filled.
        """
        if not records:
            return 0
        rows = [_security_row(r, seen_on=seen_on) for r in records]
        # A single INSERT ... ON CONFLICT cannot touch the same ISIN twice; dedupe (last wins).
        rows = list({r["isin"]: r for r in rows}.values())
        for chunk in _chunks(rows):
            stmt = pg_insert(Security).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=["isin"],
                set_={
                    "instrument_type": stmt.excluded.instrument_type,
                    "description": func.coalesce(stmt.excluded.description, Security.description),
                    "issuer": func.coalesce(stmt.excluded.issuer, Security.issuer),
                    "coupon": func.coalesce(stmt.excluded.coupon, Security.coupon),
                    "interest_type": func.coalesce(
                        stmt.excluded.interest_type, Security.interest_type
                    ),
                    "maturity_date": func.coalesce(
                        stmt.excluded.maturity_date, Security.maturity_date
                    ),
                    "face_value": func.coalesce(stmt.excluded.face_value, Security.face_value),
                    "source": stmt.excluded.source,
                    # Track the true observation window even when dates arrive out of order
                    # (e.g. backfilling an older snapshot after a newer one).
                    "first_seen": func.least(Security.first_seen, stmt.excluded.first_seen),
                    "last_seen": func.greatest(Security.last_seen, stmt.excluded.last_seen),
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
        rows = [_security_row(r, seen_on=seen_on) for r in records]
        rows = list({r["isin"]: r for r in rows}.values())
        inserted = 0
        for chunk in _chunks(rows):
            stmt = (
                pg_insert(Security)
                .values(list(chunk))
                .on_conflict_do_nothing(index_elements=["isin"])
                # RETURNING emits only actually-inserted rows, giving a true insert count —
                # the driver reports rowcount -1 for this statement shape.
                .returning(Security.isin)
            )
            inserted += len(self._session.execute(stmt).all())
        return inserted

    def enrich_missing(self, records: list[SecurityRecord]) -> int:
        """Coalesce-fill NULL reference fields on existing securities (never overwrites a value).

        Used to backfill coupon/maturity/issuer from an enrichment source (BondCentral) onto rows
        an upstream (CDSL) left sparse. Returns the number of rows touched.
        """
        filled = 0
        for r in records:
            # Only touch rows where THIS record fills a gap: for each field the record actually
            # carries, the stored value must be NULL. Anything else is a no-op UPDATE that bumps
            # updated_at, writes a dead tuple, and inflates the audited "enriched" count.
            gap_conditions = [
                column.is_(None)
                for column, value in (
                    (Security.coupon, r.coupon),
                    (Security.maturity_date, r.maturity_date),
                    (Security.issuer, r.issuer),
                    (Security.interest_type, r.interest_type),
                    (Security.face_value, r.face_value),
                )
                if value is not None
            ]
            if not gap_conditions:
                continue  # the record carries nothing fillable
            result = self._session.execute(
                update(Security)
                .where(Security.isin == r.isin, or_(*gap_conditions))
                .values(
                    coupon=func.coalesce(Security.coupon, r.coupon),
                    maturity_date=func.coalesce(Security.maturity_date, r.maturity_date),
                    issuer=func.coalesce(Security.issuer, r.issuer),
                    interest_type=func.coalesce(Security.interest_type, r.interest_type),
                    face_value=func.coalesce(Security.face_value, r.face_value),
                )
            )
            if isinstance(result, CursorResult):
                filled += max(result.rowcount, 0)
        return filled

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

    def record_attribute_bulk(
        self, attribute: str, values: dict[str, str | None], *, effective: dt.date, source: str
    ) -> int:
        """SCD-2 many ISINs for one ``attribute`` in a single pass.

        Loads the currently-open rows for the batch's ISINs (chunked queries), diffs in memory,
        and writes only genuine changes. A ``None`` value records a withdrawal — but only when the
        open row was set by the *same* source (see :func:`_apply_scd2`), and only when withdrawals
        are not anomalously widespread: one flaky upstream night serving empty ratings for the
        whole universe would otherwise write thousands of junk close/reopen rows, so a batch whose
        withdrawal count exceeds ``max(20, 10%)`` of its size has ALL withdrawals suppressed
        (individual value changes still apply) and a loud warning emitted.

        Returns:
            The number of changed values recorded.
        """
        if not values:
            return 0
        isins = list(values)
        current: dict[str, SecurityAttributeHistory] = {}
        for chunk in _chunks(isins):
            open_rows = (
                self._session.execute(
                    select(SecurityAttributeHistory).where(
                        SecurityAttributeHistory.attribute == attribute,
                        SecurityAttributeHistory.valid_to.is_(None),
                        SecurityAttributeHistory.isin.in_(list(chunk)),
                    )
                )
                .scalars()
                .all()
            )
            current.update({row.isin: row for row in open_rows})

        withdrawals = sum(
            1
            for isin, value in values.items()
            if value is None
            and (row := current.get(isin)) is not None
            and row.value is not None
            and row.source == source
        )
        suppress_withdrawals = withdrawals > max(20, len(values) // 10)
        if suppress_withdrawals:
            logger.warning(
                "scd2.mass_withdrawal_suppressed",
                attribute=attribute,
                source=source,
                withdrawals=withdrawals,
                batch=len(values),
            )

        changes = 0
        for isin, value in values.items():
            if value is None and suppress_withdrawals:
                continue
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
        """Upsert the terminal audit record for a run (re-running a day overwrites it).

        A SUCCESS row is sticky: a later failed/skipped re-run of the same day must not
        downgrade it to ``failed``/``rows=0`` — the successfully-ingested rows are still in the
        database, and drift baselines (``previous_row_count``) would otherwise lose the day.
        A success always overwrites (refreshing the row count).

        The one exception is a success that ingested **nothing**. It has no rows to protect, and
        letting it stick is what makes a mis-recorded empty day permanent: ``catch-up`` resumes
        from the last successful run, so the date is never re-attempted and no later skip can
        correct the record. Connectors now raise ``DataUnavailable`` instead of returning an
        empty batch, but the rows written before that fix still have to be able to heal.
        """
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
            where=(
                (stmt.excluded.status == "success")
                | (IngestionRun.status != "success")
                | (IngestionRun.rows_ingested == 0)
            ),
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

    def dataset_progress(
        self, source: str, *, skip_retry_cutoff: dt.date
    ) -> dict[str, DatasetProgress]:
        """Per-dataset gap-fill facts for every dataset ``source`` has ever written.

        The catch-up anchor must be computed *per dataset* (a source like FBIL writes independent
        ``fbil.gsec``/``fbil.sdl`` runs per day — one product failing must not let the other's
        success advance the anchor past it) and must re-attempt days that did not terminally
        succeed:

        * ``processed_through`` — most recent ``run_date`` with a terminal success or skip; skips
          (holidays/no-data days) count as processed so they are not retried forever.
        * ``earliest_failed`` — oldest day still marked failed. A failed day would otherwise be
          shadowed forever once any later day succeeded (``max`` alone never looks back).
        * ``earliest_recent_skip`` — oldest skip on/after ``skip_retry_cutoff``. FBIL answers
          HTTP 500 both for holidays and for "not published yet", so a recent skip may just be
          a premature run and deserves a few re-attempts before it becomes terminal.
        """
        rows = self._session.execute(
            select(
                IngestionRun.dataset,
                func.max(
                    case((IngestionRun.status.in_(("success", "skipped")), IngestionRun.run_date))
                ),
                func.min(case((IngestionRun.status == "failed", IngestionRun.run_date))),
                func.min(
                    case(
                        (
                            and_(
                                IngestionRun.status == "skipped",
                                IngestionRun.run_date >= skip_retry_cutoff,
                            ),
                            IngestionRun.run_date,
                        )
                    )
                ),
            )
            .where(IngestionRun.source == source)
            .group_by(IngestionRun.dataset)
        ).all()
        return {
            dataset: DatasetProgress(
                processed_through=processed,
                earliest_failed=failed,
                earliest_recent_skip=recent_skip,
            )
            for dataset, processed, failed, recent_skip in rows
        }


@dataclass(frozen=True, slots=True)
class DatasetProgress:
    """Gap-fill facts for one (source, dataset) — see ``dataset_progress``."""

    processed_through: dt.date | None
    """Most recent run_date with terminal success/skip (``None`` if never processed)."""
    earliest_failed: dt.date | None
    """Oldest run_date still marked failed (``None`` if none)."""
    earliest_recent_skip: dt.date | None
    """Oldest skipped run_date on/after the retry cutoff (``None`` if none)."""


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
                    "first_seen": func.least(RbiAuction.first_seen, stmt.excluded.first_seen),
                    "last_seen": func.greatest(RbiAuction.last_seen, stmt.excluded.last_seen),
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
        for chunk in _chunks(rows):
            stmt = pg_insert(EtlFileMetric).values(list(chunk))
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
        for chunk in _chunks(rows):
            stmt = pg_insert(DataQualityCheck).values(list(chunk))
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


class RbiAuctionResultRepository:
    """Persist per-security auction outcomes (idempotent per prid + security)."""

    _FIELDS: Final = (
        "source",
        "auction_date",
        "auction_type",
        "tenor_note",
        "notified_amount_cr",
        "bids_received_count",
        "bids_received_amount_cr",
        "bids_accepted_count",
        "bids_accepted_amount_cr",
        "cut_off_price",
        "cut_off_yield",
        "wavg_price",
        "wavg_yield",
        "partial_allotment_pct",
    )

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, results: list[RbiAuctionResultRecord]) -> int:
        """Insert or refresh auction outcomes.

        A re-parse overwrites: RBI occasionally reposts a release with a correction, and the
        press release is the record of truth — there is no "previous belief" worth keeping for a
        primary-market result the way there is for a daily mark.
        """
        if not results:
            return 0
        rows = [
            {"prid": r.prid, "security": r.security, **{f: getattr(r, f) for f in self._FIELDS}}
            for r in results
        ]
        # One release can list the same security twice when a state re-issues in two tranches;
        # keep the last, matching the upsert's own semantics.
        rows = list({(r["prid"], r["security"]): r for r in rows}.values())
        written = 0
        for chunk in _chunks(rows):
            stmt = pg_insert(RbiAuctionResult).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["prid", "security"],
                set_={f: getattr(stmt.excluded, f) for f in self._FIELDS},
            )
            self._session.execute(stmt)
            written += len(chunk)
        return written
