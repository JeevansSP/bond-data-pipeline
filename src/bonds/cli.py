"""Command-line interface for the bonds pipeline (``uv run bonds ...``)."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Annotated

import typer

from bonds import __version__
from bonds.config import get_settings
from bonds.logging import configure_logging, get_logger
from bonds.pipelines import (
    PipelineResult,
    RunStatus,
    SovereignValuationPipeline,
    UniversePipeline,
)
from bonds.storage import Database

app = typer.Typer(add_completion=False, help="Indian bond market data pipelines.")
db_app = typer.Typer(help="Database bootstrap/maintenance.")
ingest_app = typer.Typer(help="Run ingestion pipelines.")
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")

logger = get_logger("bonds.cli")


def _init_logging() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@db_app.command("init")
def db_init() -> None:
    """Create all tables (local bootstrap; Alembic migrations are the source of truth)."""
    _init_logging()
    Database().create_all()
    typer.echo("✔ schema created")


def _summarise(results: list[PipelineResult], *, label: str) -> None:
    counts = Counter(r.status for r in results)
    total_rows = sum(r.rows for r in results)
    typer.echo(
        f"{label}: {counts.get(RunStatus.SUCCESS, 0)} ok, "
        f"{counts.get(RunStatus.SKIPPED, 0)} skipped, "
        f"{counts.get(RunStatus.FAILED, 0)} failed, {total_rows} rows"
    )
    if counts.get(RunStatus.FAILED, 0):
        raise typer.Exit(code=1)


@ingest_app.command("universe")
def ingest_universe(
    as_of: Annotated[
        dt.datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Snapshot date (default: today)."),
    ] = None,
    max_pages: Annotated[
        int | None,
        typer.Option(help="Cap pages fetched (smoke run; omit for the full universe)."),
    ] = None,
) -> None:
    """Upsert the corporate securities-master universe (BondCentral) + rating history."""
    _init_logging()
    day = (as_of or dt.datetime.now(dt.UTC)).date()
    result = UniversePipeline(Database()).run(day, max_pages=max_pages)
    _summarise([result], label=f"universe {day.isoformat()}")


@ingest_app.command("sovereign-valuation")
def ingest_sovereign_valuation(
    date: Annotated[
        dt.datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Business date (default: today)."),
    ] = None,
) -> None:
    """Ingest FBIL G-Sec/SDL price & YTM for a single date."""
    _init_logging()
    day = (date or dt.datetime.now(dt.UTC)).date()
    results = SovereignValuationPipeline(Database()).run_date(day)
    _summarise(results, label=f"sovereign-valuation {day.isoformat()}")


@ingest_app.command("sovereign-valuation-backfill")
def backfill_sovereign_valuation(
    start: Annotated[
        dt.datetime, typer.Option(formats=["%Y-%m-%d"], help="Start date (inclusive).")
    ],
    end: Annotated[dt.datetime, typer.Option(formats=["%Y-%m-%d"], help="End date (inclusive).")],
) -> None:
    """Backfill FBIL sovereign valuations across a date range (weekdays; holidays auto-skip)."""
    _init_logging()
    results = SovereignValuationPipeline(Database()).backfill(start.date(), end.date())
    _summarise(results, label=f"backfill {start.date()}..{end.date()}")


if __name__ == "__main__":
    app()
