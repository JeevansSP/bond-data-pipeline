"""CLI smoke tests: helpers, argument validation, and command wiring (no DB, no network)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from bonds import __version__
from bonds.cli import _VERDICT, _day, _today, app
from bonds.quality.checks import Level

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_command_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("db", "ingest", "dq"):
        assert group in result.output


def test_today_is_ist_not_utc() -> None:
    # Between 00:00 and 05:30 IST these differ; the CLI must be on the Indian market clock.
    assert _today() == dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()


def test_day_prefers_explicit_option() -> None:
    assert _day(dt.datetime(2026, 7, 1)) == dt.date(2026, 7, 1)
    assert _day(None) == _today()


def test_backfill_rejects_reversed_range_cleanly() -> None:
    for command in ("sovereign-valuation-backfill", "ccil-trades-backfill"):
        result = runner.invoke(
            app, ["ingest", command, "--start", "2026-07-20", "--end", "2026-07-10"]
        )
        assert result.exit_code == 2  # clean usage error, not a traceback
        assert "is after --end" in result.output
        assert "Traceback" not in result.output


def test_verdict_never_renders_a_failed_check_as_pass() -> None:
    # Every (level, passed=False) combination must have an explicit non-pass rendering;
    # the .get() fallback to green "pass" is only for passing ERROR/WARN checks.
    for level in Level:
        assert (level, False) in _VERDICT, f"failed {level} check would render as pass"
