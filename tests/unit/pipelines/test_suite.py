"""Tests for the ingest suite definition and outcome summary."""

from __future__ import annotations

import datetime as dt

from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.pipelines.suite import default_suite, summarize
from bonds.storage import Database

DATE = dt.date(2026, 7, 18)


def _result(status: RunStatus, rows: int = 0) -> PipelineResult:
    return PipelineResult(DATE, "x", status, rows=rows)


def test_summarize_aggregates_statuses_and_rows() -> None:
    outcome = summarize(
        [
            _result(RunStatus.SUCCESS, 100),
            _result(RunStatus.SUCCESS, 50),
            _result(RunStatus.SKIPPED),
            _result(RunStatus.FAILED),
        ]
    )
    assert outcome.ok == 2
    assert outcome.skipped == 1
    assert outcome.failed == 1
    assert outcome.rows == 150
    assert outcome.has_failure


def test_summarize_clean_run_has_no_failure() -> None:
    outcome = summarize([_result(RunStatus.SUCCESS, 10)])
    assert not outcome.has_failure


def test_default_suite_covers_every_daily_source() -> None:
    # Construction only (no DB connection / network); lambdas are not invoked here.
    steps = default_suite(Database(), DATE)
    labels = [s.label for s in steps]
    assert labels == [
        "Universe · BondCentral",
        "Sovereign valuations · FBIL",
        "Public issues · SEBI",
        "Auctions · RBI",
        "Corp trades · NSE",
        "G-Sec/T-Bill trades · CCIL",
    ]
