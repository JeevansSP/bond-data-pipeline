#!/usr/bin/env bash
#
# Daily idempotent ingest runner for the bonds pipeline.
#
# Runs `bonds ingest catch-up`, which gap-fills every missed business day for the date-series
# sources (FBIL valuations, CCIL trades) and refreshes the snapshot sources for today. Safe to run
# repeatedly and after the machine has been offline — all writes are idempotent upserts.
#
# Invoked by launchd (macOS) or systemd (Linux); see scripts/README.md.

set -euo pipefail

# Schedulers start with a minimal PATH — add the usual homes for uv and docker.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

LOG_DIR="$REPO_DIR/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/ingest-$(date +%Y-%m-%d).log"
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*" | tee -a "$LOG_FILE"; }

# --- single-instance lock (mkdir is atomic on POSIX; steal it only if the holder has died) ---
LOCK_DIR="$REPO_DIR/data/.ingest.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -f "$LOCK_DIR/pid" ] && kill -0 "$(cat "$LOCK_DIR/pid" 2>/dev/null)" 2>/dev/null; then
    log "another ingest is running (pid $(cat "$LOCK_DIR/pid")); exiting"
    exit 0
  fi
  log "clearing stale lock"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
echo "$$" >"$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# --- ensure the Postgres container is up and accepting connections ---
if command -v docker >/dev/null 2>&1; then
  log "starting Postgres container"
  docker compose up -d postgres >>"$LOG_FILE" 2>&1 || log "WARN: 'docker compose up' failed — is Docker running?"
  for i in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -q >/dev/null 2>&1; then
      log "Postgres ready"
      break
    fi
    [ "$i" -eq 30 ] && log "WARN: Postgres not ready after 60s; attempting ingest anyway"
    sleep 2
  done
else
  log "WARN: docker not on PATH; assuming Postgres is already reachable"
fi

# --- run the idempotent catch-up ingest (reads .env from the repo dir) ---
log "=== bonds ingest catch-up ==="
if uv run bonds ingest catch-up >>"$LOG_FILE" 2>&1; then
  log "ingest completed OK"
  date '+%Y-%m-%dT%H:%M:%S%z' >"$LOG_DIR/last-success.txt"
else
  code=$?
  log "ingest FAILED (exit $code) — see $LOG_FILE"
  exit "$code"
fi
