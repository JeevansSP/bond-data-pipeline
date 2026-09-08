# bonds-pipeline

Daily data pipelines for the Indian bond market — sovereign (G-Sec / SDL / T-Bill / STRIPS) and
corporate, from raw exchange/benchmark feeds to an audited Postgres warehouse. Originally built
for a hold-to-maturity **ladder-strategy backtest**; now also the base for regulatory-valuation
use cases (RBI investment-portfolio Directions), so price history is **bitemporal** and every
load is audited.

Source-by-source data mapping (endpoints, schemas, quirks) lives in [`docs/research/`](docs/research).

## What's in the warehouse

| Table | Grain | Sources | Depth |
|---|---|---|---|
| `securities` | one row per ISIN (~56k) | BondCentral, CDSL, FBIL, NSE bond report | current state |
| `security_attribute_history` | SCD-2 effective-dated values (rating, day-count convention, coupon frequency, next coupon date, status…) | BondCentral, CDSL, NSE bond report | per attribute |
| `valuations` | per-ISIN daily price/YTM, **append-only bitemporal** (restatements close the old row via `superseded_at`, never overwrite) | FBIL (G-Sec, SDL, STRIPS incl. Special/UDAY sheets) | 2021 → |
| `yield_curves` | tenor point per curve per day, bitemporal | FBIL (G-Sec Par Yield, GOI ZCYC, SDL ZCYC) | 2023 → |
| `trades` | per-ISIN session summary | CCIL NDS-OM (2002 →), NSE CBM daily archive (2008 →), NSE live segments | 2002 → |
| `corporate_trades` | **one row per transaction** (RFQ + OTC-reported) | NSE Trade & Settlement (2014 →), BSE Trade & Settlement (2020 →) | ~4.5M rows |
| `public_issues`, `rbi_auctions` | primary-market calendars | SEBI, RBI | current |
| `rbi_auction_results` | one row per security per auction: notified/accepted amounts, cut-off price & yield, weighted average | RBI press releases (G-Sec, T-Bill, SDL layouts) | current |
| `security_liquidity` | **derived** — per-ISIN traded liquidity as at a business date: last print, prints & days traded over 1/3/12m, turnover, the 15-day-cap and active-market verdicts | computed from `corporate_trades` + `trades` | per as-of date |
| `corporate_spread_matrix` | **derived** — daily rating × tenor spread grid on FIMMDA's buckets, from actual prints | computed from `corporate_trades` + `yield_curves` | per quote date |
| `ingestion_runs`, `etl_file_metrics`, `data_quality_checks` | audit trail of every load, ETL funnel, persisted DQ verdicts | — | per run |

Every raw artifact (xlsx/CSV/JSON) is landed verbatim under `data/raw/<source>/` before parsing;
loads are lake-first, so backfills re-read the lake instead of re-downloading.

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
├── sources/             # one connector per provider: fbil, bondcentral, cdsl, ccil,
│                        #   nse (live), nse_cbm, nse_trade_settlement, nse_bond_report,
│                        #   bse, sebi, rbi
├── storage/             # schema (ORM) · database (engine/session) · repositories
│                        #   (upsert / SCD-2 / bitemporal supersede / replace-window)
├── pipelines/           # orchestration per dataset + suite.py (daily) + catchup.py (scheduler)
├── states.py            # canonical Indian state/UT vocabulary for SDL issuer identity
├── sovereign.py         # sovereign security classification (UDAY/DISCOM/special) + RBI spreads
├── curves.py            # yield-curve lookup at an arbitrary maturity (interpolated)
├── valuation.py         # the RBI curve-plus-spread valuation rule, as an engine
└── quality/             # per-batch checks + DB-wide assessment (`bonds dq assess`)
migrations/              # Alembic
scripts/                 # run_daily_ingest.sh + launchd/systemd units (see scripts/README.md)
tests/                   # unit/ (no DB) + integration/ (needs Postgres, `-m integration`)
data/                    # gitignored: raw data lake + Postgres volume + logs
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

# what the scheduler runs: gap-fill every missed day + refresh snapshots, then exit
uv run bonds ingest catch-up

# individual pipelines (each also has a *-backfill variant where it's a date series)
uv run bonds ingest universe                     # BondCentral securities master (~25k ISINs)
uv run bonds ingest sovereign-valuation          # FBIL G-Sec/SDL/STRIPS prices & YTM
uv run bonds ingest yield-curves                 # FBIL Par Yield / GOI ZCYC / SDL ZCYC
uv run bonds ingest corporate-trades             # trade-level BSE day + NSE window
uv run bonds ingest nse-bond-report              # day count / coupon dates / listing status
uv run bonds ingest ccil-trades --date 2026-07-10
uv run bonds ingest rbi-auction-results               # primary-market cut-offs per security

# derived products, recomputed from the tapes (also run at the end of catch-up)
uv run bonds ingest liquidity                         # 15-day-cap + active-market verdicts
uv run bonds ingest spread-matrix                     # trade-derived rating x tenor spreads
```

### Regulatory valuation

`bonds.valuation` implements the RBI curve-plus-spread rule: the central-government yield at
equivalent maturity plus a rating-graded spread for corporate bonds (capped by a traded price
from the last 15 days), or a class-specific spread for special sovereign paper — UDAY, DISCOM,
state-serviced and the GoI oil/fertiliser/FCI/recapitalisation family, classified from the
description by `bonds.sovereign`.

**The rating-graded spread grid is a placeholder.** Clause 78(1)(i)(a) is recorded in our notes
only as "CG equivalent-maturity + ≥50bp, rating-graded"; the per-rating figures were never
extracted from the Direction text. `DEFAULT_RATING_SPREAD_BP` honours the 50bp floor and steps
monotonically down the scale, which is the right shape but not the published numbers. Read the
clause and replace them before any output reaches a client, or pass your own grid to
`price_corporate`. The active-market threshold in `bonds.pipelines.liquidity` is the same kind
of placeholder for the same reason — clause 4(1) is deliberately qualitative.

Browse the data in **DBeaver** → `localhost:5418`, db/user/pass `bonds` (see `.env`).

## Scheduling

`scripts/run_daily_ingest.sh` runs `bonds ingest catch-up` under a single-instance lock: it starts
the Postgres container, holds off idle sleep (macOS `caffeinate`), ingests, then **stops the
container again** on exit. Installed as a launchd agent (13:00 IST daily + on-load catch-up after
downtime) — see [`scripts/README.md`](scripts/README.md) for launchd/systemd installation. Missed
days self-heal: `catch-up` resumes each date-series dataset from its last audited run, bounded to
30 days, and retries recent skips/failures. The half-yearly CDSL snapshot (31-Mar / 30-Sep) rides
along: `catch-up` re-attempts the latest due report date until it publishes, then leaves it alone
for six months.

**A day with no data must record `skipped`, never `success` with zero rows.** `catch-up` resumes
from the last *successful* run, so a zero-row success advances the anchor past the date and the
data is gone — no retry will ever revisit it. Every connector therefore raises `DataUnavailable`
(→ `skipped`, retried) when the source published nothing, and `SourceError` (→ `failed`) when it
returned rows that would not parse. For the same reason a connector must not land an empty
artifact in the lake: `read_or_download` would serve it back forever and no retry could heal the
gap. Both failure modes cost weeks of the sovereign and BSE tapes in August 2026; `dq assess`
now fails on either signature.

## Data quality

Two layers, both persisted:

- **Per-ingest checks** run inside every load and land in `data_quality_checks` (ISIN check-digit,
  price/YTM range, null-rate ceilings, row-count drift vs the previous run).
- **`uv run bonds dq assess`** runs warehouse-wide invariants: duplicate-key checks (current rows
  only, for the bitemporal tables), referential integrity against the securities master,
  completeness floors, consistency (zero-coupon contradictions, sparse curve days, implausible
  yields), audit reconciliation (`corporate_trades` row counts must equal what `ingestion_runs`
  recorded per loaded window), CCIL-vs-FBIL cross-source price reconciliation, closed-vocabulary
  checks (`interest_type`, SDL issuer, sovereign instrument type vs ISIN form), and **freshness**
  — every daily series must stay within 8 days of the newest date anywhere in the warehouse, and
  no date-series run may be recorded `success` with zero rows. Exits 1 on any ERROR.

`CHECK` constraints back-stop bad prices/YTMs at the DB. Use the **`active_securities`** view as
the investable universe — it excludes matured and non-ACTIVE securities (the ladder must never
hold a dead bond). For a point-in-time backtest, filter by the as-of date directly instead; for
"what did we believe on date X", query `valuations`/`yield_curves` including superseded rows.

## Development

```bash
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy --strict
make test        # unit tests + coverage floor
make test-int    # integration tests (needs Postgres up)
make check       # everything the pre-push hook runs
```
