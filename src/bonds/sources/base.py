"""Base types shared by all source connectors."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class SourceError(Exception):
    """Base class for all source-connector errors."""


class DataUnavailable(SourceError):  # noqa: N818 — deliberate name; it is a control-flow signal
    """Raised when a source has no data for a requested date (e.g. a market holiday).

    This is an expected, non-fatal condition: backfill loops catch it and skip the day
    rather than aborting.
    """


@runtime_checkable
class Source(Protocol):
    """Minimal interface every connector implements for identification/auditing."""

    name: str
    """Short stable identifier used in storage (``source`` columns) and the data-lake path."""
