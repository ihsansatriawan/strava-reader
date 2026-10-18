#!/usr/bin/env bash
# Weekly sync entry point (called by the LaunchAgent; cron-equivalent wrapper).
# Runs fetch, analyze, the progress dashboard, the story page and the Google Sheet
# mirror; a failing stage (e.g. fetch with an expired session) does not block the
# ones after it, so already-synced data is still analysed. Every stage's exit
# status is logged, and the wrapper exits 1 if any stage failed.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"
cd "$ROOT"
mkdir -p logs
echo "=== strava sync start $(date '+%F %T') ===" >> logs/cron.log
status=""
failed=0
stage() {
  local name=$1 rc
  shift
  "$PY" -u "$@" >> logs/cron.log 2>&1
  rc=$?
  status+=" $name=$rc"
  [ "$rc" -eq 0 ] || failed=1
}
stage fetch scripts/fetch.py
stage analyze scripts/analyze.py
stage progress scripts/progress.py
stage story scripts/story.py
stage sheet scripts/sheet.py --push
echo "=== strava sync done$status $(date '+%F %T') ===" >> logs/cron.log
exit "$failed"
