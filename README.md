# bonds-pipeline

Daily data pipelines for the Indian bond market — building the dataset behind a hold-to-maturity
**ladder-strategy backtest**. Sovereign-first (G-Sec / SDL / T-Bill), with corporate data collected
alongside.

Source-by-source data mapping (endpoints, schemas, quirks) lives in [`docs/research/`](docs/research).

## Pillars

1. **Current universe** — daily upsert of securities (`securities`).
2. **Attribute-change history** — SCD-2 effective-dated changes, e.g. rating/coupon
   (`security_attribute_history`).
3. **Valuation history** — per-ISIN daily price & YTM (`valuations`), from **FBIL** (implemented).

## Tech stack

- **Python 3.12**, managed **entirely with [uv](https://docs.astral.sh/uv/)**.
- **Postgres 16** via Docker Compose (data + DB volume live under the gitignored `data/`).
- **SQLAlchemy 2.0 + Alembic** (schema/migrations), **httpx + tenacity** (throttled/retrying HTTP),
  **pydantic** (models/settings), **structlog** (logging), **typer** (CLI).
- Quality gates: **ruff** (lint+format), **mypy --strict**, **pytest + coverage (≥80%)**, wired as
  **husky** git hooks.

## Layout

```
src/bonds/
├── config.py            # pydantic-settings (.env), DB URL, HTTP + data-lake config
├── logging.py           # structlog setup
├── calendar.py          # business-day iteration for backfill
├── cli.py               # `bonds` typer CLI
├── http/                # ThrottledClient (rate-limit + retry)
├── models/              # source-agnostic domain records (pydantic)
├── sources/             # one connector per provider (fbil implemented; others typed stubs)
├── storage/             # schema (ORM) · database (engine/session) · repositories (upsert/SCD-2)
└── pipelines/           # orchestration per pillar (sovereign_valuation implemented)
migrations/              # Alembic
tests/                   # unit/ (no DB) + integration/ (needs Postgres, `-m integration`)
data/                    # gitignored: raw landed files + Postgres volume
```

## Quickstart

```bash
cp .env.example .env                     # (a working .env is already present for local dev)
uv sync                                  # create venv + install deps + dev tools
npm install                              # install husky hooks (Node used ONLY for hooks)

docker compose up -d postgres            # start Postgres (volume under ./data/postgres)
uv run alembic upgrade head              # apply schema

# run the WHOLE daily suite (all sources) with a live rich progress TUI
uv run bonds ingest all                          # full run
uv run bonds ingest all --max-universe-pages 3   # smoke run

# upsert the corporate securities-master universe (BondCentral) + rating history
uv run bonds ingest universe                     # full (~25.5k ISINs, ~256 pages)
uv run bonds ingest universe --max-pages 3       # smoke run (300 bonds)

# ingest one day of FBIL sovereign valuations (G-Sec + SDL)
uv run bonds ingest sovereign-valuation --date 2026-07-10

# backfill a range (weekdays; market holidays auto-skip)
uv run bonds ingest sovereign-valuation-backfill --start 2026-07-01 --end 2026-07-10
```

Browse the data in **DBeaver** → `localhost:5432`, db/user/pass `bonds` (see `.env`).

## Data quality

Every ingest runs checks and persists them to `data_quality_checks` (ISIN check-digit, price/YTM
range, null-rate ceilings, row-count drift vs the previous run) so quality is monitored, not assumed.
`CHECK` constraints back-stop bad prices/YTMs at the DB. Use the **`active_securities`** view as the
investable universe — it excludes matured and non-ACTIVE securities (the ladder must never hold a
dead bond). For a point-in-time backtest, filter by the as-of date directly instead of the view.

## Development

```bash
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy --strict
make test        # unit tests + coverage floor
make test-int    # integration tests (needs Postgres up)
make check       # everything the pre-push hook runs
```

Scheduling is left to the OS: once the pipeline is stable, wrap
`uv run bonds ingest sovereign-valuation` in a systemd service + timer (or cron).
