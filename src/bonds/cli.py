"""Command-line interface for the bonds pipeline (``uv run bonds ...``)."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from enum import StrEnum
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from bonds import __version__
from bonds.calendar import business_days
from bonds.config import get_settings
from bonds.logging import configure_logging, get_logger
from bonds.pipelines import (
    PipelineResult,
    PublicIssuePipeline,
    RbiAuctionPipeline,
    RunStatus,
    SovereignValuationPipeline,
    TradePipeline,
    UniversePipeline,
)
from bonds.pipelines.catchup import DEFAULT_MAX_GAP_DAYS, catch_up
from bonds.pipelines.suite import StepOutcome, default_suite, summarize
from bonds.pipelines.universe import UniverseFetcher
from bonds.sources.bondcentral import BondCentralSource
from bonds.sources.ccil_historical import CcilHistoricalTradesSource, derive_securities
from bonds.sources.cdsl import CdslSource
from bonds.sources.nse import NseSource
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


def _status_text(o: StepOutcome) -> str:
    if o.has_failure:
        return f"[bold red]✗ {o.failed} failed[/] · {o.rows} rows"
    if o.skipped and not o.ok:
        return f"[yellow]⊘ skipped[/] · {o.rows} rows"
    return f"[bold green]✓ {o.ok} ok[/] · {o.rows} rows"


def _print_summary(console: Console, day: dt.date, outcomes: dict[str, StepOutcome]) -> None:
    table = Table(title=f"Ingest summary · {day.isoformat()}", title_style="bold")
    table.add_column("Stage")
    table.add_column("Result")
    table.add_column("Rows", justify="right")
    for label, o in outcomes.items():
        table.add_row(label, _status_text(o), f"{o.rows:,}")
    total_rows = sum(o.rows for o in outcomes.values())
    failed = sum(o.failed for o in outcomes.values())
    table.add_section()
    verdict = "[bold red]FAILURES[/]" if failed else "[bold green]all clean[/]"
    table.add_row("[bold]Total", verdict, f"[bold]{total_rows:,}")
    console.print(table)


@ingest_app.command("all")
def ingest_all(
    as_of: Annotated[
        dt.datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Business date (default: today)."),
    ] = None,
    max_universe_pages: Annotated[
        int | None,
        typer.Option(help="Cap BondCentral universe pages (smoke run; omit for full)."),
    ] = None,
) -> None:
    """Run the full daily ingest suite (all sources) with a live progress TUI."""
    # Quiet logs so structlog output doesn't garble the live display.
    configure_logging(level="WARNING", json=get_settings().log_json)
    day = (as_of or dt.datetime.now(dt.UTC)).date()
    console = Console()
    steps = default_suite(Database(), day, max_universe_pages=max_universe_pages)
    outcomes: dict[str, StepOutcome] = {}

    console.rule(f"[bold]Indian bond pipeline · {day.isoformat()}")
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("{task.fields[status]}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_ids = {
            s.label: progress.add_task(s.label, total=1, start=False, status="[dim]pending")
            for s in steps
        }
        for step in steps:
            tid = task_ids[step.label]
            progress.start_task(tid)
            progress.update(tid, status="[yellow]running…")
            outcome = summarize(step.run())
            outcomes[step.label] = outcome
            progress.update(tid, completed=1, status=_status_text(outcome))

    _print_summary(console, day, outcomes)
    if any(o.has_failure for o in outcomes.values()):
        raise typer.Exit(code=1)


@ingest_app.command("catch-up")
def ingest_catch_up(
    as_of: Annotated[
        dt.datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Target date (default: today)."),
    ] = None,
    max_gap_days: Annotated[
        int,
        typer.Option(help="Cap how many days back a gap-fill reaches (runaway-backfill guard)."),
    ] = DEFAULT_MAX_GAP_DAYS,
) -> None:
    """Self-healing daily run for schedulers: gap-fill missed date-series days + refresh snapshots.

    Idempotent — safe to run twice a day or after the machine was offline for several days.
    """
    _init_logging()
    day = (as_of or dt.datetime.now(dt.UTC)).date()
    report = catch_up(Database(), as_of=day, max_gap_days=max_gap_days)
    outcomes = {label: summarize(results) for label, results in report.groups.items()}
    _print_summary(Console(), day, outcomes)
    if any(o.has_failure for o in outcomes.values()):
        raise typer.Exit(code=1)


class UniverseSource(StrEnum):
    """Selectable universe source connectors."""

    bondcentral = "bondcentral"
    cdsl = "cdsl"


@ingest_app.command("universe")
def ingest_universe(
    source: Annotated[
        UniverseSource,
        typer.Option(help="Universe source connector."),
    ] = UniverseSource.bondcentral,
    as_of: Annotated[
        dt.datetime | None,
        typer.Option(
            formats=["%Y-%m-%d"],
            help="Snapshot date. BondCentral: default today. CDSL: a 31-Mar/30-Sep report date.",
        ),
    ] = None,
    max_pages: Annotated[
        int | None,
        typer.Option(help="Cap pages fetched (BondCentral smoke run; ignored for CDSL)."),
    ] = None,
) -> None:
    """Upsert a securities-master universe + attribute history (BondCentral or CDSL)."""
    _init_logging()
    day = (as_of or dt.datetime.now(dt.UTC)).date()
    connector: UniverseFetcher = (
        CdslSource() if source is UniverseSource.cdsl else BondCentralSource()
    )
    result = UniversePipeline(Database(), source=connector).run(day, max_pages=max_pages)
    _summarise([result], label=f"universe[{source.value}] {day.isoformat()}")


@ingest_app.command("public-issues")
def ingest_public_issues(
    as_of: Annotated[
        dt.datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Snapshot date (default: today)."),
    ] = None,
) -> None:
    """Ingest the SEBI corporate-bond public-issue calendar."""
    _init_logging()
    day = (as_of or dt.datetime.now(dt.UTC)).date()
    result = PublicIssuePipeline(Database()).run(day)
    _summarise([result], label=f"public-issues {day.isoformat()}")


@ingest_app.command("nse-trades")
def ingest_nse_trades(
    as_of: Annotated[
        dt.datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Snapshot date (default: today)."),
    ] = None,
) -> None:
    """Ingest NSE corporate-bond trades (latest session; forward capture)."""
    _init_logging()
    day = (as_of or dt.datetime.now(dt.UTC)).date()
    result = TradePipeline(Database(), source=NseSource()).run(day)
    _summarise([result], label=f"nse-trades {day.isoformat()}")


@ingest_app.command("ccil-trades")
def ingest_ccil_trades(
    as_of: Annotated[
        dt.datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Trade date (default: today)."),
    ] = None,
) -> None:
    """Ingest CCIL NDS-OM historical trades (G-Sec/SDL/T-Bill) for a date."""
    _init_logging()
    day = (as_of or dt.datetime.now(dt.UTC)).date()
    result = TradePipeline(
        Database(), source=CcilHistoricalTradesSource(), derive_securities=derive_securities
    ).run(day)
    _summarise([result], label=f"ccil-trades {day.isoformat()}")


@ingest_app.command("ccil-trades-backfill")
def ingest_ccil_trades_backfill(
    start: Annotated[dt.datetime, typer.Option(formats=["%Y-%m-%d"], help="Start (inclusive).")],
    end: Annotated[dt.datetime, typer.Option(formats=["%Y-%m-%d"], help="End (inclusive).")],
) -> None:
    """Backfill CCIL NDS-OM trades across a date range (weekdays; holidays return 0 rows)."""
    _init_logging()
    db = Database()
    source = CcilHistoricalTradesSource()
    pipeline = TradePipeline(db, source=source, derive_securities=derive_securities)
    results = [pipeline.run(day) for day in business_days(start.date(), end.date())]
    _summarise(results, label=f"ccil-backfill {start.date()}..{end.date()}")


@ingest_app.command("rbi-auctions")
def ingest_rbi_auctions(
    as_of: Annotated[
        dt.datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Snapshot date (default: today)."),
    ] = None,
) -> None:
    """Ingest the RBI sovereign auction calendar (recent auctions + dates + links)."""
    _init_logging()
    day = (as_of or dt.datetime.now(dt.UTC)).date()
    result = RbiAuctionPipeline(Database()).run(day)
    _summarise([result], label=f"rbi-auctions {day.isoformat()}")


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
