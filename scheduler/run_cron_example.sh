#!/usr/bin/env bash
# =============================================================================
# run_cron_example.sh -- scheduled batch wrapper for cron or Task Scheduler.
#
# This is the recommended way to run the system for real clients. GitHub Actions
# runners are ephemeral, so the per-tenant SQLite checkpoint database (which is
# what makes the human-approval queue resumable across days) has to be squeezed
# into a cache. A small always-on box keeps it on disk where it belongs.
#
# LINUX / MACOS (crontab -e):
#   # weekdays at 09:15, one line per tenant
#   15 9 * * 1-5 /path/to/leadgen_agent/scheduler/run_cron_example.sh example_tenant
#
# WINDOWS (Task Scheduler):
#   Program:   C:\Program Files\Git\bin\bash.exe
#   Arguments: -lc "/c/Users/you/Desktop/leadgen_agent/scheduler/run_cron_example.sh example_tenant"
#   Start in:  C:\Users\you\Desktop\leadgen_agent
#   Tick "Run whether user is logged on or not".
#
# WHAT THIS DOES NOT DO
#   It never approves drafts, and it cannot send LinkedIn messages. N5 pauses
#   each lead for your review; run `python -m src.cli.approve` yourself. The
#   LinkedIn queue is worked by hand through `python -m src.cli.linkedin_queue`.
# =============================================================================

set -euo pipefail

TENANT="${1:-example_tenant}"
shift || true
EXTRA_ARGS=("$@")

# Repo root, regardless of where cron was invoked from.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---- venv ------------------------------------------------------------------
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"          # Linux / macOS
elif [ -x ".venv/Scripts/python.exe" ]; then
  PYTHON=".venv/Scripts/python.exe"  # Windows (Git Bash)
else
  echo "No virtualenv found at $ROOT/.venv" >&2
  echo "Create one:  python -m venv .venv && pip install -r requirements.txt" >&2
  exit 1
fi

# ---- logging ---------------------------------------------------------------
LOG_DIR="$ROOT/.data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${TENANT}-$(date +%Y-%m-%d).log"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE"; }

# ---- one run at a time per tenant -----------------------------------------
# Two concurrent batches would double-send and race on the checkpoint database.
LOCK_FILE="$ROOT/.data/${TENANT}.lock"
mkdir -p "$(dirname "$LOCK_FILE")"

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "another run for '$TENANT' is already in progress; exiting"
    exit 0
  fi
else
  # Git Bash on Windows has no flock; fall back to a PID file.
  if [ -f "$LOCK_FILE" ] && kill -0 "$(cat "$LOCK_FILE" 2>/dev/null)" 2>/dev/null; then
    log "another run for '$TENANT' is already in progress; exiting"
    exit 0
  fi
  echo $$ > "$LOCK_FILE"
  trap 'rm -f "$LOCK_FILE"' EXIT
fi

# ---- preflight -------------------------------------------------------------
if [ ! -f ".env" ]; then
  log "WARNING: no .env found. The run will use dry-run fallbacks for every"
  log "         integration. Copy .env.example to .env and fill it in."
fi

if [ ! -f "config/tenants/${TENANT}.yaml" ]; then
  log "ERROR: no config for tenant '${TENANT}' at config/tenants/${TENANT}.yaml"
  exit 2
fi

log "starting batch for tenant '${TENANT}'"

# ---- run -------------------------------------------------------------------
# `|| true` on the batch itself is deliberate: a non-zero exit must not stop the
# summary steps below, which are how you find out what needs your attention.
set +e
"$PYTHON" -m src.cli.run_batch --tenant "$TENANT" "${EXTRA_ARGS[@]}" >>"$LOG_FILE" 2>&1
BATCH_STATUS=$?
set -e

log "batch exited with status ${BATCH_STATUS}"

# ---- what needs a human ----------------------------------------------------
log "--- drafts awaiting approval ---"
"$PYTHON" -m src.cli.approve --tenant "$TENANT" --list >>"$LOG_FILE" 2>&1 || true

log "--- LinkedIn messages awaiting manual send ---"
"$PYTHON" -m src.cli.linkedin_queue --tenant "$TENANT" list >>"$LOG_FILE" 2>&1 || true

# ---- retention -------------------------------------------------------------
# Keep 60 days of logs. Checkpoints and counters are never pruned here: a
# deleted checkpoint loses an in-flight cadence, and a deleted counter resets a
# free-tier quota you have already spent.
find "$LOG_DIR" -name '*.log' -type f -mtime +60 -delete 2>/dev/null || true

log "done. Full output: ${LOG_FILE}"
exit "$BATCH_STATUS"
