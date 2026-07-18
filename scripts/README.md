```
Scheduling the daily bonds ingest as a self-healing service.

2026-07-18_193000 : initial version (runner + launchd + systemd, idempotent catch-up)
```

# Daily ingest service

The pipeline is designed to run once a day, unattended, and **catch up on any days it missed** while
the machine was asleep or offline — without ever double-counting.

Two pieces make that work:

1. **`bonds ingest catch-up`** — the idempotent command a scheduler runs. It:
   - **gap-fills** the date-series sources (FBIL valuations, CCIL trades) for *every* missed
     business day, from the day after each source's last processed date up to today (bounded by
     `--max-gap-days`, default 30, so a fresh/idle DB never backfills years by accident);
   - **refreshes** the snapshot / latest-session sources (universe, SEBI public issues, RBI
     auctions, NSE trades) once for today.
   - Every write is an `ON CONFLICT` upsert keyed by `(source, dataset, run_date)`, so running it
     twice in a day — or after a week offline — converges instead of duplicating.

2. **`run_daily_ingest.sh`** — a wrapper the scheduler actually calls. It takes a single-instance
   lock (no overlapping runs), brings up the Postgres container and waits for it, runs the
   catch-up, and logs to `data/logs/ingest-YYYY-MM-DD.log` (plus `data/logs/last-success.txt`).

Try it by hand first:

```bash
uv run bonds ingest catch-up            # or: bash scripts/run_daily_ingest.sh
```

---

## macOS (launchd)

launchd runs a missed `StartCalendarInterval` job when the Mac next wakes or boots, so a missed
21:00 run fires on wake and the catch-up fills the gap.

```bash
# 1. Copy the agent into place (paths in the plist already point at this repo):
cp scripts/launchd/com.cydratech.bonds-ingest.plist ~/Library/LaunchAgents/

# 2. Load it (use `bootstrap` on modern macOS):
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cydratech.bonds-ingest.plist

# 3. (optional) run it right now to verify:
launchctl kickstart -k gui/$(id -u)/com.cydratech.bonds-ingest

# status / logs:
launchctl print gui/$(id -u)/com.cydratech.bonds-ingest | grep -i state
tail -f data/logs/ingest-$(date +%Y-%m-%d).log

# to remove:
launchctl bootout gui/$(id -u)/com.cydratech.bonds-ingest
```

Notes:
- Docker Desktop must be set to **start at login** (System Settings → General → Login Items), or the
  container won't be up when the job runs; the runner starts it via `docker compose up -d` but
  cannot start Docker Desktop itself.
- Change the time by editing `StartCalendarInterval` in the plist, then bootout + bootstrap again.

## Linux (systemd)

`Persistent=true` runs a missed timer as soon as the machine boots; the catch-up then fills the gap.

```bash
# adjust WorkingDirectory / ExecStart path / User in bonds-ingest.service first, then:
sudo cp scripts/systemd/bonds-ingest.service /etc/systemd/system/
sudo cp scripts/systemd/bonds-ingest.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bonds-ingest.timer

# status / logs:
systemctl list-timers bonds-ingest.timer
journalctl -u bonds-ingest.service -f
sudo systemctl start bonds-ingest.service   # run once now
```

## Backfilling a gap larger than `--max-gap-days`

The catch-up intentionally caps how far back it reaches. For a bigger hole, run the explicit
backfills once, then let the daily job maintain it:

```bash
uv run bonds ingest ccil-trades-backfill --start 2024-01-01 --end 2024-12-31
uv run bonds ingest sovereign-valuation-backfill --start 2024-01-01 --end 2024-12-31
```
