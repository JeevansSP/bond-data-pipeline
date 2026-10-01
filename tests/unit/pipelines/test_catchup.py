"""Tests for the catch-up date-window logic and orchestration."""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Any, ClassVar

import pytest

from bonds.pipelines import catchup
from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.pipelines.catchup import (
    bounded_start,
    catch_up,
    cdsl_snapshot_due,
    dataset_start,
    latest_half_yearly_snapshot,
)
from bonds.storage.repositories import DatasetProgress

AS_OF = dt.date(2026, 7, 17)

# Source instances handed to each fake pipeline, so instance sharing can be asserted.
_SOURCES_SEEN: dict[str, list[Any]] = {}


class _FakeDatabase:
    """Stands in for Database: session() yields a placeholder, no engine required."""

    def session(self) -> Any:
        return contextlib.nullcontext(object())


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

    def test_failed_day_older_than_cap_is_ignored_not_clamped(self) -> None:
        # A failed day below the floor is unreachable: clamping the start to the floor for it
        # would re-ingest a rolling 31-day window EVERY night forever while never actually
        # re-running (healing) the failed day. It must not affect the start at all.
        p = _progress(processed=dt.date(2026, 7, 16), failed=dt.date(2026, 1, 2))
        assert dataset_start(p, as_of=AS_OF, max_gap_days=30) == dt.date(2026, 7, 17)

    def test_failed_day_exactly_at_floor_is_retried(self) -> None:
        floor = AS_OF - dt.timedelta(days=30)
        p = _progress(processed=dt.date(2026, 7, 16), failed=floor)
        assert dataset_start(p, as_of=AS_OF, max_gap_days=30) == floor

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
def _backfill_pipeline(label: str, calls: dict[str, list[tuple[dt.date, dt.date]]]) -> type:
    class _Fake:
        def __init__(self, database: Any) -> None:
            pass

        def backfill(self, start: dt.date, end: dt.date) -> list[PipelineResult]:
            calls.setdefault(label, []).append((start, end))
            return [PipelineResult(start, label, RunStatus.SUCCESS, rows=1)]

    return _Fake


class _FakeTradePipeline:
    runs: ClassVar[dict[str, list[dt.date]]] = {}

    def __init__(
        self,
        database: Any,
        *,
        source: Any,
        derive_securities: Any = None,
        date_series: bool = True,
    ) -> None:
        self._name = type(source).__name__
        self.date_series = date_series

    def run(self, day: dt.date) -> PipelineResult:
        self.runs.setdefault(self._name, []).append(day)
        return PipelineResult(day, f"{self._name}.trades", RunStatus.SUCCESS, rows=1)


def _snapshot_pipeline(label: str, runs: dict[str, list[dt.date]]) -> type:
    class _Fake:
        def __init__(self, database: Any, *, source: Any = None) -> None:
            # UniversePipeline serves two connectors: BondCentral daily and CDSL half-yearly.
            self._label = label if source is None else f"{label}_{type(source).__name__}"
            _SOURCES_SEEN.setdefault(self._label, []).append(source)

        def run(self, day: dt.date) -> PipelineResult:
            runs.setdefault(self._label, []).append(day)
            return PipelineResult(day, self._label, RunStatus.SUCCESS, rows=1)

    return _Fake


@pytest.fixture
def backfill_calls() -> dict[str, list[tuple[dt.date, dt.date]]]:
    return {}


@pytest.fixture
def orchestration(
    monkeypatch: pytest.MonkeyPatch, backfill_calls: dict[str, list[tuple[dt.date, dt.date]]]
) -> dict[str, list[dt.date]]:
    """Patch every pipeline catch_up() constructs with call-recording fakes."""
    snapshot_runs: dict[str, list[dt.date]] = {}
    _FakeTradePipeline.runs = {}
    _SOURCES_SEEN.clear()
    monkeypatch.setattr(
        catchup, "SovereignValuationPipeline", _backfill_pipeline("fbil_vals", backfill_calls)
    )
    monkeypatch.setattr(
        catchup, "YieldCurvePipeline", _backfill_pipeline("fbil_curves", backfill_calls)
    )
    monkeypatch.setattr(
        catchup, "BseCorporateTradePipeline", _backfill_pipeline("bse_ct", backfill_calls)
    )
    monkeypatch.setattr(
        catchup, "NseCorporateTradePipeline", _backfill_pipeline("nse_ct", backfill_calls)
    )
    monkeypatch.setattr(
        catchup, "NseBondReportPipeline", _backfill_pipeline("nse_report", backfill_calls)
    )
    monkeypatch.setattr(catchup, "TradePipeline", _FakeTradePipeline)
    monkeypatch.setattr(catchup, "UniversePipeline", _snapshot_pipeline("universe", snapshot_runs))
    monkeypatch.setattr(
        catchup, "PublicIssuePipeline", _snapshot_pipeline("public_issues", snapshot_runs)
    )
    monkeypatch.setattr(
        catchup, "RbiAuctionPipeline", _snapshot_pipeline("rbi_auctions", snapshot_runs)
    )
    monkeypatch.setattr(catchup, "cdsl_snapshot_due", lambda db, *, as_of: None)
    monkeypatch.setattr(
        catchup, "RbiAuctionResultPipeline", _snapshot_pipeline("rbi_results", snapshot_runs)
    )
    monkeypatch.setattr(
        catchup, "LiquidityPipeline", _snapshot_pipeline("liquidity", snapshot_runs)
    )
    monkeypatch.setattr(
        catchup, "SpreadMatrixPipeline", _snapshot_pipeline("spread_matrix", snapshot_runs)
    )
    return snapshot_runs


def test_catch_up_gap_fills_series_and_refreshes_snapshots(
    orchestration: dict[str, list[dt.date]],
    backfill_calls: dict[str, list[tuple[dt.date, dt.date]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # series_start is keyed by the pipeline's first expected dataset — several pipelines
    # share a source name (fbil valuations vs curves), so the source alone is ambiguous.
    starts = {
        "fbil.gsec": dt.date(2026, 7, 16),
        "fbil.curve.gsec": dt.date(2026, 7, 16),
        "ccil.trades": dt.date(2026, 7, 15),
        "bse.corp_trades": dt.date(2026, 7, 17),
        "nse.corp_trades_ts": dt.date(2026, 7, 17),
        "nse_cbm.trades": dt.date(2026, 7, 16),
        "nse_cbr.bond_report": dt.date(2026, 7, 17),
        "derived.liquidity": dt.date(2026, 7, 16),
        "derived.spread_matrix": AS_OF,
    }
    monkeypatch.setattr(
        catchup,
        "series_start",
        lambda db, source, *, as_of, max_gap_days, expected_datasets=(), **kw: starts[
            next(iter(expected_datasets))
        ],
    )

    report = catch_up(object(), as_of=AS_OF)  # type: ignore[arg-type]

    # Backfill pipelines: one call each spanning [start, as_of].
    assert backfill_calls == {
        "fbil_vals": [(dt.date(2026, 7, 16), AS_OF)],
        "fbil_curves": [(dt.date(2026, 7, 16), AS_OF)],
        "bse_ct": [(dt.date(2026, 7, 17), AS_OF)],
        "nse_ct": [(dt.date(2026, 7, 17), AS_OF)],
        "nse_report": [(dt.date(2026, 7, 17), AS_OF)],
    }
    # Per-day trade pipelines: one run per business day in [start, as_of].
    assert _FakeTradePipeline.runs["CcilHistoricalTradesSource"] == [
        dt.date(2026, 7, 15),
        dt.date(2026, 7, 16),
        dt.date(2026, 7, 17),
    ]
    assert _FakeTradePipeline.runs["NseCbmDailySource"] == [
        dt.date(2026, 7, 16),
        dt.date(2026, 7, 17),
    ]
    # Snapshot sources: exactly one refresh each, for as_of.
    assert orchestration == {
        "universe": [AS_OF],
        "public_issues": [AS_OF],
        "rbi_auctions_RbiSource": [AS_OF],
        "rbi_results_RbiSource": [AS_OF],
        # Derived products gap-fill their missed days and recompute for as_of, *after* the
        # tapes above are current.
        "liquidity": [dt.date(2026, 7, 16), AS_OF],
        "spread_matrix": [AS_OF],
    }
    # The NSE live feed is not part of the scheduled run: mid-session it is not a session
    # summary, and the CBM archive + trade-level report carry the finished session.
    assert "NseSource" not in _FakeTradePipeline.runs
    assert "Corp trades · NSE" not in report.groups
    # The report flattens all group results.
    assert report.as_of == AS_OF
    # 5 backfills + 3 ccil days + 2 cbm days + 4 snapshot runs + 3 derived runs
    assert len(report.results) == 5 + 3 + 2 + 4 + 3


def test_catch_up_with_nothing_to_gap_fill_still_refreshes_snapshots(
    orchestration: dict[str, list[dt.date]],
    backfill_calls: dict[str, list[tuple[dt.date, dt.date]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every series already current: start is tomorrow (> as_of) -> zero gap-fill work.
    monkeypatch.setattr(
        catchup,
        "series_start",
        lambda db, source, *, as_of, max_gap_days, **kw: AS_OF + dt.timedelta(days=1),
    )

    report = catch_up(object(), as_of=AS_OF)  # type: ignore[arg-type]

    assert backfill_calls == {}
    assert "CcilHistoricalTradesSource" not in _FakeTradePipeline.runs
    assert "NseCbmDailySource" not in _FakeTradePipeline.runs
    assert report.groups["Sovereign valuations · FBIL"] == []
    assert report.groups["Yield curves · FBIL"] == []
    assert report.groups["G-Sec/T-Bill trades · CCIL"] == []
    assert report.groups["Corp trades (trade-level) · BSE"] == []
    assert report.groups["Corp trades (trade-level) · NSE"] == []
    assert report.groups["Corp trades (daily archive) · NSE"] == []
    assert report.groups["Bond master · NSE report"] == []
    # Derived products still recompute as_of even with nothing to gap-fill.
    assert orchestration["liquidity"] == [AS_OF]
    assert orchestration["spread_matrix"] == [AS_OF]
    assert len(report.results) == 6  # snapshot + derived runs only


def test_derived_products_always_recompute_as_of_even_on_a_sunday(
    orchestration: dict[str, list[dt.date]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Sunday run whose derived series resume *on* Sunday: business_days() yields no Sunday,
    # but as_of must still be recomputed — the tapes it summarises were refreshed moments ago.
    sunday = dt.date(2026, 7, 19)
    assert sunday.weekday() == 6
    monkeypatch.setattr(
        catchup, "series_start", lambda db, source, *, as_of, max_gap_days, **kw: sunday
    )

    catch_up(object(), as_of=sunday)  # type: ignore[arg-type]

    assert orchestration["liquidity"] == [sunday]
    assert orchestration["spread_matrix"] == [sunday]


def test_derived_products_gap_fill_missed_days_including_saturday(
    orchestration: dict[str, list[dt.date]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two missed runs (Sat 18th, then the Sunday is no market day) before a Monday run: both the
    # Saturday and Monday as-of views are computed, oldest first.
    monday = dt.date(2026, 7, 20)
    monkeypatch.setattr(
        catchup,
        "series_start",
        lambda db, source, *, as_of, max_gap_days, **kw: dt.date(2026, 7, 18),
    )

    catch_up(object(), as_of=monday)  # type: ignore[arg-type]

    assert orchestration["liquidity"] == [dt.date(2026, 7, 18), monday]
    assert orchestration["spread_matrix"] == [dt.date(2026, 7, 18), monday]


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        # Between snapshots -> the 31-Mar one is the latest due.
        (dt.date(2026, 9, 7), dt.date(2026, 3, 31)),
        # On the report date itself.
        (dt.date(2026, 9, 30), dt.date(2026, 9, 30)),
        (dt.date(2026, 10, 1), dt.date(2026, 9, 30)),
        # The day before 31-Mar still points at last September.
        (dt.date(2026, 3, 30), dt.date(2025, 9, 30)),
        (dt.date(2026, 3, 31), dt.date(2026, 3, 31)),
        # Early January reaches back across the year boundary.
        (dt.date(2026, 1, 5), dt.date(2025, 9, 30)),
    ],
)
def test_latest_half_yearly_snapshot(as_of: dt.date, expected: dt.date) -> None:
    assert latest_half_yearly_snapshot(as_of) == expected


class _FakeResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _CountingDatabase:
    """Stands in for Database: session().execute() reports how many successful loads exist."""

    def __init__(self, successful_loads: int) -> None:
        self._successful_loads = successful_loads

    def session(self) -> Any:
        outer = self

        class _Session:
            def execute(self, *_a: Any, **_k: Any) -> _FakeResult:
                return _FakeResult(outer._successful_loads)

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

        return _Session()


@pytest.mark.parametrize(
    ("successful_loads", "expected"),
    [
        # Never loaded successfully -> the latest due snapshot.
        (0, dt.date(2026, 3, 31)),
        # Already loaded -> nothing to do for six months.
        (1, None),
    ],
)
def test_cdsl_snapshot_due(successful_loads: int, expected: dt.date | None) -> None:
    db = _CountingDatabase(successful_loads)
    assert cdsl_snapshot_due(db, as_of=dt.date(2026, 9, 7)) == expected  # type: ignore[arg-type]


def test_a_skipped_cdsl_attempt_stays_due() -> None:
    """Regression: an unpublished report date must remain due after being skipped.

    The check originally keyed on ``dataset_progress().processed_through``, which is the newest
    run_date with a success **or a skip**. CDSL posts the 31-Mar/30-Sep file days late, so the
    first attempt recorded SKIPPED at the snapshot's own run_date, advanced that anchor to the
    snapshot, and every later night then saw "already processed" — missing the file for six
    months, the exact staleness the scheduling was added to fix. Keying on a *successful* load
    is what makes the retry real, and a skip count of zero successes proves it.
    """
    db = _CountingDatabase(0)
    assert cdsl_snapshot_due(db, as_of=dt.date(2026, 9, 7)) == dt.date(2026, 3, 31)  # type: ignore[arg-type]


def test_catch_up_runs_the_due_cdsl_snapshot(
    orchestration: dict[str, list[dt.date]],
    backfill_calls: dict[str, list[tuple[dt.date, dt.date]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = dt.date(2026, 3, 31)
    monkeypatch.setattr(catchup, "cdsl_snapshot_due", lambda db, *, as_of: snapshot)
    monkeypatch.setattr(
        catchup,
        "series_start",
        lambda db, source, *, as_of, max_gap_days, **kw: AS_OF + dt.timedelta(days=1),
    )

    report = catch_up(object(), as_of=AS_OF)  # type: ignore[arg-type]

    # The CDSL snapshot runs for its own report date, not for as_of.
    assert orchestration["universe_CdslSource"] == [snapshot]
    assert orchestration["universe"] == [AS_OF]
    assert len(report.groups["Amount outstanding · CDSL"]) == 1


def test_both_rbi_pipelines_share_one_source(
    orchestration: dict[str, list[dt.date]],
    backfill_calls: dict[str, list[tuple[dt.date, dt.date]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both RBI pipelines must be handed the same connector instance.

    Each sweeps the same undated press-release index and then fetches every entry's detail page.
    The source caches those bodies per instance, so sharing it is what turns three passes over
    rbi.org.in per release into one; two instances would silently restore the duplicate traffic.
    """
    monkeypatch.setattr(
        catchup,
        "series_start",
        lambda db, source, *, as_of, max_gap_days, **kw: AS_OF + dt.timedelta(days=1),
    )

    catch_up(object(), as_of=AS_OF)  # type: ignore[arg-type]

    calendar = _SOURCES_SEEN["rbi_auctions_RbiSource"][0]
    results = _SOURCES_SEEN["rbi_results_RbiSource"][0]
    assert calendar is results
