```
Scheduling the daily bonds ingest as a self-healing service.

2026-07-18_193000 : initial version (runner + launchd + systemd, idempotent catch-up)
2026-07-21_112001 : hardened lock (acquisition race, PID reuse), 30-day log pruning, raised systemd timeout, documented launchd log-dir prerequisite
2026-07-26_153131 : runner wraps the ingest in caffeinate -i on macOS (idle sleep stretched a ~5-min run across 16.8 h on 2026-07-25)
2026-07-30_234500 : runner stops the Postgres container on exit when it started it (no more 24/7 DB for a 7-minute job)
```

# Daily ingest service

The pipeline is designed to run once a day, unattended, and **catch up on any days it missed** while
the machine was asleep or offline — without ever double-counting.

Two pieces make that work:

1. **`bonds ingest catch-up`** — the idempotent command a scheduler runs. It:
   - **gap-fills** the date-series sources (FBIL valuations, CCIL trades) for *every* missed
     business day, from the day after each source's last processed date up to today (bounded by
     `--max-gap-days`, default 30, so a fresh/idle DB never backfills years by accident);
   - **refreshes** the snapshot sources (universe, SEBI public issues, RBI auctions) once for
     today. The NSE *live* trade feed is not in the scheduled run: at 13:00 it shows a session in
     progress, and the connector now refuses to record that as a session summary.
   - Every write is an `ON CONFLICT` upsert keyed by `(source, dataset, run_date)`, so running it
     twice in a day — or after a week offline — converges instead of duplicating.

2. **`run_daily_ingest.sh`** — a wrapper the scheduler actually calls. It takes a single-instance
   lock (no overlapping runs), brings up the Postgres container and waits for it, runs the
   catch-up (under `caffeinate -i` on macOS so idle sleep can't suspend it mid-run), and logs to
   `data/logs/ingest-YYYY-MM-DD.log` (plus `data/logs/last-success.txt`). On exit — success or
   failure — it stops the container again (`docker compose stop`), but only if this run started
   it, so the database isn't left running 24/7 for a ~7-minute daily job.

Try it by hand first:

```bash
uv run bonds ingest catch-up            # or: bash scripts/run_daily_ingest.sh
```

---

## macOS (launchd)

launchd runs a missed `StartCalendarInterval` job when the Mac next wakes or boots, so a missed
13:00 run fires on wake and the catch-up fills the gap.

The agent fires at **13:00 local**, before most sources publish (FBIL ~19:00, CCIL and BSE after
the 17:00 close), so each run ingests the *previous* business day and the current day lands
tomorrow. That only works because a source with nothing to give records `skipped` and is
re-attempted — catch-up resumes from the last **successful** run, so a connector that reports
`success` with zero rows (or that lands an empty artifact the lake then serves back) makes the
day unreachable forever. See the warning in the plist comment; `bonds dq assess` fails on both
signatures.

```bash
# 0. Make sure the log directory exists — launchd does NOT create intermediate directories
#    for StandardOutPath/StandardErrorPath, so the very first run's launchd-level output
#    would be lost on a fresh clone otherwise:
mkdir -p data/logs

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
- Docker Desktop does not have to be running: if the daemon is down the runner launches it
  (`open -g -a Docker`) and waits up to 3 minutes before `docker compose up -d`. Before this
  existed, the 2026-09-17 and 2026-09-18 runs died on a stopped Docker after a restart. Setting
  Docker Desktop to **start at login** (System Settings → General → Login Items) still saves the wait.
- A failed run posts a macOS notification (`bonds ingest FAILED`) as well as writing the log — a
  failure that only reaches `launchd.out.log` goes unnoticed for days.
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
