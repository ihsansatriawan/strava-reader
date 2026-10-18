#!/usr/bin/env bash
# Open a visible Chrome on the automation profile so you can log into Strava once.
# Use this when scripts/fetch.py reports SESSION EXPIRED.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pkill -f "user-data-dir=$ROOT/chrome-data" 2>/dev/null || true
sleep 1
exec "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$ROOT/chrome-data" \
  --remote-debugging-port=9222 \
  --no-first-run --no-default-browser-check \
  "https://www.strava.com/login"
