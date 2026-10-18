#!/usr/bin/env bash
# Weekly sync entry point (called by the LaunchAgent; cron-equivalent wrapper).
# Runs fetch, analyze, and the progress dashboard; a fetch failure (e.g.
# session expired) does not block analysis of already-synced data.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/opt/homebrew/bin/python3
cd "$ROOT"
mkdir -p logs
echo "=== strava sync start $(date '+%F %T') ===" >> logs/cron.log
"$PY" -u scripts/fetch.py >> logs/cron.log 2>&1
fetch_rc=$?
"$PY" -u scripts/analyze.py >> logs/cron.log 2>&1
"$PY" -u scripts/progress.py >> logs/cron.log 2>&1
echo "=== strava sync done fetch_rc=$fetch_rc $(date '+%F %T') ===" >> logs/cron.log
