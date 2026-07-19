"""Unit tests for the assessment report aggregation."""

from __future__ import annotations

from bonds.quality.assessment import AssessmentReport
from bonds.quality.checks import Level, QualityCheck


def _c(name: str, level: Level, passed: bool) -> QualityCheck:
    return QualityCheck(name, level, passed=passed)


def test_report_flattens_and_flags_error() -> None:
    report = AssessmentReport(
        groups={
            "A": [_c("ok", Level.INFO, True), _c("bad", Level.ERROR, False)],
            "B": [_c("warn", Level.WARN, False)],
        }
    )
    assert len(report.checks) == 3
    assert report.has_error
    assert report.has_warning


def test_report_clean_has_no_error_or_warning() -> None:
    report = AssessmentReport(
        groups={"A": [_c("ok", Level.INFO, True), _c("also_ok", Level.ERROR, True)]}
    )
    assert not report.has_error
    assert not report.has_warning
