# rea-strava

Sync, analyze, and visualize **your own Strava data** — locally, on a schedule,
without a Strava subscription.

Everything here runs against the same endpoints your signed-in browser uses,
authenticated with your own session. No API keys, no OAuth apps, no subscription.

## What you get

| Artifact | What it is |
|---|---|
| `data/` | Full archive: JSON summaries of every activity, the raw `.fit` your watch uploaded, and GPX exports (GPS + elevation + heart rate) |
| `reports/latest.md` | Training report: totals by sport, weekly trend, pace trend, HR table, records |
| `reports/progress.html` | Visual dashboard: volume bars, pace trend, HR-efficiency scatter, long-run growth, consistency calendar, computed insights |
| `diagrams/` | Three diagrams explaining exactly how the whole mechanism works |

## How it works

The signed-in Strava web app was traced via CDP (Chrome DevTools Protocol).
For a logged-in free account these endpoints are all accessible:

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /athlete/training_activities?page=N` (with `X-Requested-With: XMLHttpRequest` header) | free | JSON summaries of ALL your activities (distance, time, elevation, ...) |
| `GET /activities/{id}/export_original` | free | the raw file your watch uploaded (FIT) |
| `GET /activities/{id}/export_gpx` | free | GPX with GPS + elevation + heart rate |
| `GET /activities/{id}/streams` | **paywalled (403)** | website analysis streams — not needed, the exports contain the same raw data |

Auth is the ordinary web session cookie `_strava4_session`, kept alive inside a
dedicated Chrome profile (`chrome-data/`). `scripts/fetch.py`:

1. Launches that profile headless with CDP on port `9222` (starts it if down).
2. Verifies the session is still logged in (redirect to `/login` ⇒ expired).
3. Syncs the current `_strava4_session` cookie via the `agent-browser` CLI.
4. Fetches everything over plain HTTP with that cookie (fast), with an
   in-browser fallback if Cloudflare interferes.
5. Downloads only what's new (`data/downloads.json` tracks state).

## Requirements

- macOS (the launchd scheduling and cookie paths are macOS-specific; the Python
  scripts themselves run anywhere Chrome runs)
- Google Chrome
- Python 3.8+ — **stdlib only**, no `pip install` needed
- [agent-browser](https://github.com/agent-browser/agent-browser) CLI: `npm i -g agent-browser`

## Setup

### 1. Clone the repo

```bash
git clone <this-repo> && cd rea-strava
```

### 2. Create the automation profile (`chrome-data/`)

This is a dedicated Chrome profile that holds your Strava login. Pick one:

**Option A — log in once (simplest).** Run the helper and log into Strava in
the window that opens; that's the only manual step in the entire system:

```bash
scripts/relogin.sh   # opens a visible Chrome on chrome-data/ -> log in once -> done
```

**Option B — clone an already-logged-in profile.** If your daily Chrome is
already signed into Strava, copy the session bits without touching your
browser (cookie DB location varies by Chrome version — check both):

```bash
mkdir -p chrome-data/Default
SRC="$HOME/Library/Application Support/Google/Chrome"
cp "$SRC/Local State" chrome-data/
cp "$SRC/Default/Preferences" chrome-data/Default/
python3 - <<'PY'
import sqlite3, os
src_db = os.path.expanduser("~/Library/Application Support/Google/Chrome")
for cand in ("Default/Cookies", "Default/Network/Cookies"):
    p = os.path.join(src_db, cand)
    if os.path.exists(p):
        s = sqlite3.connect(p)
        d = sqlite3.connect("chrome-data/Default/Cookies")
        s.backup(d); d.close(); s.close()
        print("copied:", cand)
PY
```

Cookies are encrypted, but the decryption key lives in your macOS Keychain, so
the cloned profile logs in automatically on the same machine.

### 3. First sync

```bash
python3 scripts/fetch.py
```

Watch it paginate through `/athlete/training_activities` and download every
`.fit` + `.gpx`. Your data now lives in `data/` (~40 MB for a few years of
activities).

### 4. Read your data

```bash
python3 scripts/analyze.py               # -> reports/latest.md (all-time)
python3 scripts/analyze.py --months 4    # -> reports/report-<date>-4mo.md (recent window)
python3 scripts/progress.py              # -> reports/progress.html (dashboard)
open reports/progress.html
```

How to read the dashboard:

- **Volume mingguan** — bars are km/week; the line is the 4-week rolling
  average. Ramp rate is your injury-risk dial.
- **Tren pace** — every dot is one run (higher = faster); the red line is the
  5-run rolling median. This is your fitness curve.
- **Efisiensi jantung** — average HR vs pace, pale = older, dark = recent.
  Points drifting *down* at the same pace means real aerobic gains.
- **Kalender konsistensi** — weeks × days; `L` marks long runs (≥ 8 km).
- The **insights list** at the top is computed from your data (pace delta,
  HR at matched pace, ramp rate, consistency %) — not hand-written.

### 5. Schedule it

A macOS LaunchAgent runs the full pipeline **weekly, Sunday 22:00**:

```bash
cat > ~/Library/LaunchAgents/com.ihsan.strava-sync.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.ihsan.strava-sync</string>
    <key>ProgramArguments</key>
    <array><string>/absolute/path/to/rea-strava/scripts/sync.sh</string></array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>0</integer>
        <key>Hour</key><integer>22</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardErrorPath</key>
    <string>/absolute/path/to/rea-strava/logs/launchd.err</string>
</dict>
</plist>
PLIST
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ihsan.strava-sync.plist
```

- Manual run now: `launchctl kickstart gui/$(id -u)/com.ihsan.strava-sync`
  (or just `scripts/sync.sh`)
- Logs: `logs/cron.log`
- Change the schedule: edit `StartCalendarInterval` (`Weekday 0` = Sunday),
  then `launchctl bootout` + `bootstrap` again.
- Remove: `launchctl bootout gui/$(id -u)/com.ihsan.strava-sync && rm ~/Library/LaunchAgents/com.ihsan.strava-sync.plist`

## Layout

```
scripts/fetch.py      # sync: session + summaries + FIT/GPX downloads
scripts/analyze.py    # markdown report (all-time or --months N)
scripts/progress.py   # progress.html dashboard
scripts/sync.sh       # pipeline wrapper for the scheduler
scripts/relogin.sh    # visible Chrome for the one manual login
chrome-data/          # automation Chrome profile (your Strava session)  [ignored]
data/                 # your data                                          [ignored]
reports/              # generated reports                                  [ignored]
logs/                 # scheduler output                                   [ignored]
diagrams/             # mechanism diagrams (public-safe)
```

## Troubleshooting

- **`SESSION EXPIRED`** (fetch exits 2, writes `data/SESSION_EXPIRED`):
  run `scripts/relogin.sh`, log in once, re-run. Nothing else to fix.
- **Cloudflare block**: `fetch.py` automatically retries the request inside
  the logged-in browser (a real Chrome fetch) — no action needed.
- **`crontab -` hangs**: on some macOS setups writing crontabs stalls; that's
  why this repo uses launchd.
- **Homebrew Python `pyexpat` broken** (`ImportError: No module named expat`):
  the analyzer parses GPX with regex on purpose; you shouldn't hit this.

## Safety & privacy

- `chrome-data/` holds a live Strava session — **treat it like a password**.
- `data/` is your full location history. `reports/` is your training log.
- These directories, plus `.env`, `logs/`, and `.claude/`, are all in
  `.gitignore` and must never be committed or published.
- This repository itself (scripts, diagrams, docs) contains no personal data.
- Educational, personal-use project: it reads only **your own** data, fetched
  the same way your browser does. Not affiliated with Strava. Automating access
  to your account may sit in a gray area of the Strava Terms of Service — use
  it responsibly and at your own discretion.

## Credits

- Mechanism diagrams built with the
  [diagram-design](https://github.com/cathrynlavery/diagram-design) skill.
