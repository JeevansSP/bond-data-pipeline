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
# `|| true`: under set -eo pipefail an unwritable LOG_FILE (permissions/disk full) would
# otherwise kill the script on the very first log line, with no diagnostic anywhere.
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*" | tee -a "$LOG_FILE" || true; }

# --- single-instance lock (mkdir is atomic on POSIX; steal it only if the holder has died) ---
# flock(1) isn't on stock macOS, so this stays a mkdir lock, hardened against the races that
# matter when RunAtLoad and the 21:00 timer fire together:
#   * a fresh lock with no pid file yet means the holder is mid-acquisition -> treat as HELD
#     (only steal a pid-less lock after a generous grace period, i.e. the holder crashed);
#   * verify a live pid actually belongs to this script (PID reuse after reboot otherwise
#     fakes "another ingest is running" and silently skips the day);
#   * losing the steal race (our mkdir fails) exits cleanly instead of dying via set -e.
LOCK_DIR="$REPO_DIR/data/.ingest.lock"
LOCK_GRACE_SECONDS=300

lock_mtime() { stat -f %m "$LOCK_DIR" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || echo 0; }

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  holder_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -z "$holder_pid" ]; then
    age=$(( $(date +%s) - $(lock_mtime) ))
    if [ "$age" -lt "$LOCK_GRACE_SECONDS" ]; then
      log "another ingest is acquiring the lock; exiting"
      exit 0
    fi
  elif kill -0 "$holder_pid" 2>/dev/null \
      && ps -p "$holder_pid" -o command= 2>/dev/null | grep -Eq "bash .*run_daily_ingest\.sh"; then
    # The command-line anchor matters: a bare name match would false-positive on PID reuse by
    # e.g. `vim run_daily_ingest.sh` and silently skip the day's ingest.
    log "another ingest is running (pid $holder_pid); exiting"
    exit 0
  fi
  # Steal by atomic rename: of two concurrent stealers exactly one wins the mv; the loser's
  # mv fails and it exits. A naive rm+mkdir here lets the loser's rm destroy the winner's
  # freshly-acquired lock (and the winner's EXIT trap then destroys a third runner's lock).
  if ! mv "$LOCK_DIR" "$LOCK_DIR.stale.$$" 2>/dev/null; then
    log "another process is stealing the stale lock; exiting"
    exit 0
  fi
  rm -rf "$LOCK_DIR.stale.$$"
  log "cleared stale lock"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "another process took the lock; exiting"
    exit 0
  fi
fi
echo "$$" >"$LOCK_DIR/pid"

# --- cleanup: always release the lock; stop Postgres only if this run started it ---
DOCKER_STARTED=0
cleanup() {
  if [ "$DOCKER_STARTED" -eq 1 ]; then
    log "stopping Postgres container"
    docker compose stop postgres >>"$LOG_FILE" 2>&1 || log "WARN: 'docker compose stop' failed"
  fi
  rm -rf "$LOCK_DIR"
}
trap cleanup EXIT

# --- prune old ingest logs (the dated files grow unbounded otherwise) ---
find "$LOG_DIR" -name 'ingest-*.log' -mtime +30 -delete 2>/dev/null || true
# launchd's own stdout/stderr files are appended forever and launchd never rotates them;
# truncate in place (deleting would orphan launchd's open file descriptor).
for lf in "$LOG_DIR"/launchd.out.log "$LOG_DIR"/launchd.err.log; do
  if [ -f "$lf" ] && [ "$(wc -c <"$lf" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    : >"$lf"
  fi
done

# --- make sure the Docker daemon itself is up ---
# On macOS the daemon lives inside Docker Desktop, a GUI app that only runs once launched: after
# a reboot (or a logout) it is simply not there, and `docker compose up` fails instantly. That
# killed the 2026-09-17 and 2026-09-18 scheduled runs while the operator was travelling — two
# days of "is Docker running?" in the log and nothing else. A LaunchAgent runs in the user's GUI
# session, so it may launch the app itself; the daemon then takes ~10-40s to accept connections.
DOCKER_START_TIMEOUT_SECONDS=180
ensure_docker_daemon() {
  docker info >/dev/null 2>&1 && return 0
  if [ "$(uname)" = "Darwin" ] && [ -d /Applications/Docker.app ]; then
    log "Docker daemon not running; launching Docker Desktop"
    open -g -a Docker || log "WARN: 'open -a Docker' failed"
    for _ in $(seq 1 $((DOCKER_START_TIMEOUT_SECONDS / 2))); do
      docker info >/dev/null 2>&1 && { log "Docker daemon ready"; return 0; }
      sleep 2
    done
    log "WARN: Docker daemon not ready after ${DOCKER_START_TIMEOUT_SECONDS}s"
  else
    log "WARN: Docker daemon not running and cannot be started from here"
  fi
  return 1
}

# --- surface a failure to the operator (macOS Notification Center) ---
# A failure that only reaches the log files is a failure nobody sees until they look.
notify_failure() {
  command -v osascript >/dev/null 2>&1 || return 0
  osascript -e "display notification \"$1\" with title \"bonds ingest FAILED\"" \
    >/dev/null 2>&1 || true
}

# --- ensure the Postgres container is up and accepting connections ---
if command -v docker >/dev/null 2>&1; then
  ensure_docker_daemon || true  # fall through: the ingest fails loudly below if it is still down
  log "starting Postgres container"
  if docker compose up -d postgres >>"$LOG_FILE" 2>&1; then
    DOCKER_STARTED=1  # stop it in cleanup, even if the ingest itself fails
  else
    log "WARN: 'docker compose up' failed — is Docker running?"
  fi
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

# --- hold off idle sleep for the duration of the ingest (macOS) ---
# The ingest is long and network-bound, so idle sleep suspends it mid-flight: on 2026-07-25 a
# BondCentral universe pull that normally finishes in ~5 minutes spanned 16.8 hours of wall clock
# across repeated sleep/wake cycles (the data still landed — every write is an idempotent upsert —
# but the run held the lock all night). `caffeinate -i` blocks *idle* system sleep only while the
# ingest runs; it cannot stop a lid close or an explicit sleep, which the catch-up self-heals from
# on the next run. Absent on Linux, where the systemd unit runs unattended anyway.
KEEP_AWAKE=""
if command -v caffeinate >/dev/null 2>&1; then
  KEEP_AWAKE="caffeinate -i"
fi

# --- run the idempotent catch-up ingest (reads .env from the repo dir) ---
log "=== bonds ingest catch-up ${KEEP_AWAKE:+(sleep held off) }==="
# $KEEP_AWAKE is deliberately unquoted: it is a fixed two-word literal (or empty, which must
# expand to nothing rather than an empty argument).
# shellcheck disable=SC2086
if $KEEP_AWAKE uv run bonds ingest catch-up >>"$LOG_FILE" 2>&1; then
  log "ingest completed OK"
  date '+%Y-%m-%dT%H:%M:%S%z' >"$LOG_DIR/last-success.txt"
else
  code=$?
  log "ingest FAILED (exit $code) — see $LOG_FILE"
  notify_failure "exit $code — see data/logs/ingest-$(date +%Y-%m-%d).log"
  exit "$code"
fi
