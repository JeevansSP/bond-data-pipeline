"""NSE bond-report pipeline: cash-flow reference fields into the securities master.

The daily Corporate Bond Report is a *secondary* master: BondCentral remains authoritative
for identity, so this pipeline never overwrites — it ``insert_missing``-es unseen ISINs,
coalesce-fills NULL reference gaps (coupon/maturity/issuer/face value), and SCD-2-tracks the
fields no other source carries (day count convention, coupon frequency, next coupon date,
floating benchmark/spread, NSE's rating and listing status).

Attribute naming: NSE's ``Status`` is tracked as ``listing_status`` (not ``security_status``)
because its vocabulary ("Listed") differs from BondCentral's ("ACTIVE") — sharing the key
would record a spurious change row every day the two sources alternate.

Backfills must run chronologically (SCD-2 skips out-of-order effective dates).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from bonds.calendar import business_days
from bonds.pipelines.base import PipelineResult, execute_run, persist_file_metrics
from bonds.sources.nse_bond_report import NseBondReportSource
from bonds.storage import Database
from bonds.storage.repositories import SecurityRepository

# Attributes this feed owns in security_attribute_history. ``issuance_date`` is shared with
# CDSL deliberately: an issue date is immutable, so agreeing sources no-op and a disagreement
# is a genuine data conflict worth a history row.
REPORT_ATTRIBUTES: tuple[str, ...] = (
    "credit_rating_nse",
    "step_up_coupons",
    "coupon_frequency",
    "next_coupon_date",
    "day_count_convention",
    "floating_benchmark",
    "benchmark_spread",
    "issuance_date",
    "listing_status",
)


class NseBondReportPipeline:
    """Ingest the NSE bond report into securities gaps + attribute history."""

    def __init__(self, database: Database, source: NseBondReportSource | None = None) -> None:
        self._db = database
        self._source = source or NseBondReportSource()

    def run_date(self, date: dt.date) -> PipelineResult:
        """Ingest one day's report."""
        dataset = f"{self._source.name}.bond_report"

        def work(session: Session) -> int:
            records = list(self._source.iter_records(date))
            repo = SecurityRepository(session)
            inserted = repo.insert_missing(records, seen_on=date)
            filled = repo.enrich_missing(records)
            changes = 0
            for attribute in REPORT_ATTRIBUTES:
                values = {r.isin: r.attributes.get(attribute) for r in records}
                changes += repo.record_attribute_bulk(
                    attribute, values, effective=date, source=self._source.name
                )
            persist_file_metrics(
                session, self._source, source=self._source.name, dataset=dataset, run_date=date
            )
            return inserted + filled + changes

        return execute_run(
            self._db, source=self._source.name, dataset=dataset, run_date=date, work=work
        )

    def backfill(self, start: dt.date, end: dt.date) -> list[PipelineResult]:
        """Ingest chronologically across ``[start, end]`` (SCD-2 needs date order)."""
        return [self.run_date(day) for day in business_days(start, end)]
