"""Tests for the catch-up date-window logic and orchestration."""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

import pytest

from bonds.pipelines import catchup
from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.pipelines.catchup import bounded_start, catch_up, dataset_start
from bonds.storage.repositories import DatasetProgress

AS_OF = dt.date(2026, 7, 17)


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        # Never ingested -> start at the floor (as_of - max_gap_days), not the beginning of time.
        (None, dt.date(2026, 6, 17)),
        # Ran yesterday -> resume today.
        (dt.date(2026, 7, 16), dt.date(2026, 7, 17)),
        # Ran a few days ago -> resume the day after (gap-fill the missed days).
        (dt.date(2026, 7, 13), dt.date(2026, 7, 14)),
        # Already ran today -> start is tomorrow, i.e. > as_of => "nothing to do".
        (dt.date(2026, 7, 17), dt.date(2026, 7, 18)),
        # Long outage beyond the cap -> clamp to the floor (runaway-backfill guard).
        (dt.date(2026, 1, 1), dt.date(2026, 6, 17)),
    ],
)
def test_bounded_start(anchor: dt.date | None, expected: dt.date) -> None:
    assert bounded_start(anchor, as_of=AS_OF, max_gap_days=30) == expected


def test_bounded_start_already_current_is_after_as_of() -> None:
    # The catch-up treats start > as_of as "no gap"; assert the boundary explicitly.
    assert bounded_start(AS_OF, as_of=AS_OF, max_gap_days=30) > AS_OF


def _progress(
    processed: dt.date | None = None,
    failed: dt.date | None = None,
    recent_skip: dt.date | None = None,
) -> DatasetProgress:
    return DatasetProgress(
        processed_through=processed, earliest_failed=failed, earliest_recent_skip=recent_skip
    )


class TestDatasetStart:
    def test_resumes_after_processed_when_nothing_to_retry(self) -> None:
        p = _progress(processed=dt.date(2026, 7, 15))
        assert dataset_start(p, as_of=AS_OF, max_gap_days=30) == dt.date(2026, 7, 16)

    def test_failed_day_shadowed_by_later_success_is_retried(self) -> None:
        # Tue failed, Wed-Fri succeeded: the old max-anchor logic would resume Sat and lose Tue.
        p = _progress(processed=dt.date(2026, 7, 17), failed=dt.date(2026, 7, 14))
        assert dataset_start(p, as_of=AS_OF, max_gap_days=30) == dt.date(2026, 7, 14)

    def test_failed_day_older_than_cap_is_clamped_to_floor(self) -> None:
        p = _progress(processed=dt.date(2026, 7, 16), failed=dt.date(2026, 1, 2))
        assert dataset_start(p, as_of=AS_OF, max_gap_days=30) == dt.date(2026, 6, 17)

    def test_recent_skip_is_retried(self) -> None:
        # Premature same-day run recorded a skip before the source published; the evening's
        # scheduled run must re-attempt today rather than compute "nothing to do".
        p = _progress(processed=AS_OF, recent_skip=AS_OF)
        assert dataset_start(p, as_of=AS_OF, max_gap_days=30) == AS_OF

    def test_old_skips_are_terminal(self) -> None:
        # earliest_recent_skip is None for skips beyond the retry cutoff (the repository query
        # filters them) -> holidays are not refetched forever.
        p = _progress(processed=dt.date(2026, 7, 16), recent_skip=None)
        assert dataset_start(p, as_of=AS_OF, max_gap_days=30) == dt.date(2026, 7, 17)

    def test_never_processed_starts_at_floor(self) -> None:
        assert dataset_start(_progress(), as_of=AS_OF, max_gap_days=30) == dt.date(2026, 6, 17)


# ---------------------------------------------------------------- catch_up() orchestration
class _FakeSovereignPipeline:
    calls: ClassVar[list[tuple[dt.date, dt.date]]] = []

    def __init__(self, database: Any) -> None:
        pass

    def backfill(self, start: dt.date, end: dt.date) -> list[PipelineResult]:
        self.calls.append((start, end))
        return [PipelineResult(start, "fbil.gsec", RunStatus.SUCCESS, rows=1)]


class _FakeTradePipeline:
    runs: ClassVar[dict[str, list[dt.date]]] = {}

    def __init__(self, database: Any, *, source: Any, derive_securities: Any = None) -> None:
        self._name = type(source).__name__

    def run(self, day: dt.date) -> PipelineResult:
        self.runs.setdefault(self._name, []).append(day)
        return PipelineResult(day, f"{self._name}.trades", RunStatus.SUCCESS, rows=1)


def _snapshot_pipeline(label: str, runs: dict[str, list[dt.date]]) -> type:
    class _Fake:
        def __init__(self, database: Any) -> None:
            pass

        def run(self, day: dt.date) -> PipelineResult:
            runs.setdefault(label, []).append(day)
            return PipelineResult(day, label, RunStatus.SUCCESS, rows=1)

    return _Fake


@pytest.fixture
def orchestration(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dt.date]]:
    """Patch every pipeline catch_up() constructs with call-recording fakes."""
    snapshot_runs: dict[str, list[dt.date]] = {}
    _FakeSovereignPipeline.calls = []
    _FakeTradePipeline.runs = {}
    monkeypatch.setattr(catchup, "SovereignValuationPipeline", _FakeSovereignPipeline)
    monkeypatch.setattr(catchup, "TradePipeline", _FakeTradePipeline)
    monkeypatch.setattr(catchup, "UniversePipeline", _snapshot_pipeline("universe", snapshot_runs))
    monkeypatch.setattr(
        catchup, "PublicIssuePipeline", _snapshot_pipeline("public_issues", snapshot_runs)
    )
    monkeypatch.setattr(
        catchup, "RbiAuctionPipeline", _snapshot_pipeline("rbi_auctions", snapshot_runs)
    )
    return snapshot_runs


def test_catch_up_gap_fills_series_and_refreshes_snapshots(
    orchestration: dict[str, list[dt.date]], monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = {"fbil": dt.date(2026, 7, 16), "ccil": dt.date(2026, 7, 15)}
    monkeypatch.setattr(
        catchup,
        "series_start",
        lambda db, source, *, as_of, max_gap_days, skip_retry_days=3: starts[source],
    )

    report = catch_up(object(), as_of=AS_OF)  # type: ignore[arg-type]

    # FBIL: one backfill spanning [start, as_of].
    assert _FakeSovereignPipeline.calls == [(dt.date(2026, 7, 16), AS_OF)]
    # CCIL: one run per business day in [start, as_of] (Wed 15th .. Fri 17th).
    assert _FakeTradePipeline.runs["CcilHistoricalTradesSource"] == [
        dt.date(2026, 7, 15),
        dt.date(2026, 7, 16),
        dt.date(2026, 7, 17),
    ]
    # Snapshot sources: exactly one refresh each, for as_of.
    assert orchestration == {
        "universe": [AS_OF],
        "public_issues": [AS_OF],
        "rbi_auctions": [AS_OF],
    }
    assert _FakeTradePipeline.runs["NseSource"] == [AS_OF]
    # The report flattens all group results.
    assert report.as_of == AS_OF
    assert len(report.results) == 1 + 3 + 4  # fbil backfill + 3 ccil days + 4 snapshots


def test_catch_up_with_nothing_to_gap_fill_still_refreshes_snapshots(
    orchestration: dict[str, list[dt.date]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both series already current: start is tomorrow (> as_of) -> zero gap-fill work.
    monkeypatch.setattr(
        catchup,
        "series_start",
        lambda db, source, *, as_of, max_gap_days, skip_retry_days=3: AS_OF + dt.timedelta(days=1),
    )

    report = catch_up(object(), as_of=AS_OF)  # type: ignore[arg-type]

    assert _FakeSovereignPipeline.calls == []
    assert "CcilHistoricalTradesSource" not in _FakeTradePipeline.runs
    assert report.groups["Sovereign valuations · FBIL"] == []
    assert report.groups["G-Sec/T-Bill trades · CCIL"] == []
    assert len(report.results) == 4  # snapshots only
