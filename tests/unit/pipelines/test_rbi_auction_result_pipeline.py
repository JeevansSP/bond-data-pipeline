"""Tests for the RBI auction-result pipeline's orchestration (no network, no DB)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from bonds.models import RbiAuctionRecord, RbiAuctionResultRecord
from bonds.pipelines import rbi_auction_result as module
from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.pipelines.rbi_auction_result import RbiAuctionResultPipeline
from bonds.sources.base import DataUnavailable

AS_OF = dt.date(2026, 9, 4)


class _FakeSource:
    name = "rbi"

    def __init__(self, results: list[RbiAuctionResultRecord]) -> None:
        self._results = results
        self.auctions_called = 0
        self.results_called = 0

    def fetch_auctions(self, as_of: dt.date) -> list[RbiAuctionRecord]:
        self.auctions_called += 1
        return [
            RbiAuctionRecord(
                prid="1",
                title="Government Stock - Full Auction Results",
                auction_type="G-Sec",
                source="rbi",
            )
        ]

    def fetch_auction_results(
        self, auctions: list[RbiAuctionRecord]
    ) -> list[RbiAuctionResultRecord]:
        assert auctions, "results must be fetched from the calendar just refreshed"
        self.results_called += 1
        return self._results


def _run(monkeypatch: pytest.MonkeyPatch, source: _FakeSource) -> tuple[PipelineResult, list[Any]]:
    """Run the pipeline against a stubbed repository; return the result and what it wrote."""
    written: list[Any] = []

    class _FakeRepo:
        def __init__(self, session: Any) -> None:
            pass

        def upsert_many(self, records: list[RbiAuctionResultRecord]) -> int:
            written.extend(records)
            return len(records)

    monkeypatch.setattr(module, "RbiAuctionResultRepository", _FakeRepo)
    monkeypatch.setattr(module, "persist_file_metrics", lambda *a, **k: None)

    captured: dict[str, PipelineResult] = {}

    def _fake_execute_run(
        database: Any, *, source: str, dataset: str, run_date: dt.date, work: Any
    ) -> PipelineResult:
        try:
            rows = work(object())
        except DataUnavailable as exc:
            captured["r"] = PipelineResult(run_date, dataset, RunStatus.SKIPPED, message=str(exc))
        else:
            captured["r"] = PipelineResult(run_date, dataset, RunStatus.SUCCESS, rows=rows)
        return captured["r"]

    monkeypatch.setattr(module, "execute_run", _fake_execute_run)
    return RbiAuctionResultPipeline(object(), source=source).run(AS_OF), written  # type: ignore[arg-type]


def test_results_are_upserted(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _FakeSource(
        [
            RbiAuctionResultRecord(
                prid="1", security="New GS 2031", source="rbi", notified_amount_cr=21000.0
            ),
            RbiAuctionResultRecord(
                prid="1", security="7.71% GS 2066", source="rbi", notified_amount_cr=11000.0
            ),
        ]
    )
    result, written = _run(monkeypatch, source)
    assert result.status is RunStatus.SUCCESS
    assert result.rows == 2
    assert source.auctions_called == 1 and source.results_called == 1
    assert {r.security for r in written} == {"New GS 2031", "7.71% GS 2066"}


def test_a_day_with_no_auction_is_skipped_not_recorded_as_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Most days publish no auction result. That is "nothing yet", not "processed" — a SUCCESS
    # with zero rows would bank the day and stop any retry from ever revisiting it.
    result, written = _run(monkeypatch, _FakeSource([]))
    assert result.status is RunStatus.SKIPPED
    assert written == []
