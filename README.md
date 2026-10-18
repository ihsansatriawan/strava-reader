# strava-reader

Sync, analyze, and visualize **your own Strava data** — locally, on a schedule,
without a Strava subscription.

Everything here runs against the same endpoints your signed-in browser uses,
authenticated with your own session. No API keys, no OAuth apps, no subscription.

## What you get

| Artifact | What it is |
|---|---|
| `data/` | Archive of every activity **since 2026-01-01**: JSON summaries, the raw `.fit` your device uploaded, and GPX exports (GPS + elevation + heart rate) |
| `reports/latest.md` | Training report since 2026-01-01: totals by sport, this week vs 4-week average, last 12 weeks, monthly run pace, HR table, records |
| `reports/report-<date>-<N>mo.md` | The same report for the last N months (`--months N`) |
| `reports/progress.html` | Rolling dashboard of runs since June 2026: volume bars, pace trend, HR-efficiency scatter, distance per run, consistency calendar, computed insights |
| `reports/story.html` | The story of every run since July 2026, read point by point from GPX (see below) |
| Google Sheet | Optional mirror of the data in six tabs (activities, FIT device totals, run analysis, km splits, weekly load, notes), rewritten whole on every sync (see step 7) |
| `diagrams/` | Three diagrams explaining exactly how the whole mechanism works |

## Data scope: 2026 only

`scripts/config.py` holds one constant, `DATA_SINCE = datetime(2026, 1, 1)`,
used by every script:

- `fetch.py` still pages through all summaries (it is cheap), but stores only
  activities starting on or after `DATA_SINCE` in `data/activities.json` and
  downloads FIT/GPX only for those.
- `analyze.py`, `progress.py` and `story.py` read only activities from
  `DATA_SINCE` onward, so older files already in `data/` are ignored without a
  re-sync. They are not deleted.

Change the year in that one place to widen or narrow the scope. Start dates
are compared in the activity's local wall-clock time.

## How it works

The signed-in Strava web app was traced via CDP (Chrome DevTools Protocol).
For a logged-in free account these endpoints are all accessible:

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /athlete/training_activities?page=N` (with `X-Requested-With: XMLHttpRequest` header) | free | JSON summaries of ALL your activities (distance, time, elevation, ...) |
| `GET /activities/{id}/export_original` | free | the raw file your device uploaded (FIT) |
| `GET /activities/{id}/export_gpx` | free | GPX with GPS + elevation + heart rate |
| `GET /activities/{id}/streams` | **paywalled (403)** | website analysis streams — not needed, the exports contain the same raw data |

Auth is the ordinary web session cookie `_strava4_session`, kept alive inside a
dedicated Chrome profile (`chrome-data/`). `scripts/fetch.py`:

1. Launches that profile headless with CDP on port `9222` (starts it if down).
2. Verifies the session is still logged in (redirect to `/login` ⇒ expired).
3. Syncs the current `_strava4_session` cookie via the `agent-browser` CLI.
4. Fetches everything over plain HTTP with that cookie (fast), with an
   in-browser fallback if Cloudflare interferes.
5. Keeps only activities since `DATA_SINCE` and downloads only what's new
   (`data/downloads.json` tracks each export — FIT and GPX — on its own; one
   that failed or arrived truncated is retried next sync, a 404 is final).
6. Tells a dead login (redirect to `/login`, HTTP 401, the login form → exit
   2, `data/SESSION_EXPIRED`) apart from Strava itself failing (HTTP 5xx, an
   error page → exit 1, `STRAVA ERROR`, nothing to fix: the next sync retries).

### How the data is cleaned

- **Distance and moving time come from the watch, not Strava's recount.**
  Strava recomputes a watch upload's distance from its GPS points; when the
  watch GPS stalls, that recount runs short (up to ~28% on some Huawei runs)
  and the stalled stretch counts as "stopped". So for every activity whose FIT
  file was written by a device (anything but Strava's own phone recorder),
  `scripts/fit.py` reads the device's own session distance, and moving time is
  rebuilt from its speed series (≥ 1 m/s for runs, ≥ 0.5 m/s otherwise).
  Strava's number is kept as `strava_km`. `story.html` also re-times its
  per-point analysis on the watch's speed series when the GPS track misses the
  watch total by > 3% — totals are right then, but single km are estimates,
  so per-km bests are left out for those runs.
- **Duplicate uploads are merged.** If both a phone app and a watch upload the
  same run, Strava keeps both. Every report collapses activities of the same
  sport that start within 3 minutes of each other into one (another sport
  starting in between doesn't split them), keeping the copy with heart rate
  (the watch), then the earlier upload. Without this, totals double-count. Once
  you record with the watch only, there is nothing to merge.
- **A broken file doesn't break a report.** A truncated FIT, or one holding
  several sessions, falls back to Strava's summary; a GPX without a usable
  track still counts in totals, weekly load and the calendar, it just has no
  point-by-point charts.
- **Local time is kept as local time.** Strava's `start_date_local_raw` is the
  wall-clock time encoded as a UTC epoch; it is decoded as UTC so the machine's
  timezone can't shift an evening run into the next day (and the next week).
- **GPX is parsed with regex on purpose** — Homebrew Python's `pyexpat` can be
  broken against the system libexpat, and Strava GPX is machine-generated.

## Requirements

- macOS (the launchd scheduling and cookie paths are macOS-specific; the Python
  scripts themselves run anywhere Chrome runs)
- Google Chrome
- Python 3.8+ — **stdlib only**, no `pip install` needed
- [agent-browser](https://github.com/agent-browser/agent-browser) CLI: `npm i -g agent-browser`

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/ihsansatriawan/strava-reader.git && cd strava-reader
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

Watch it paginate through `/athlete/training_activities` and download the
`.fit` + `.gpx` of every activity since `DATA_SINCE`. Your data now lives in
`data/`.

### 4. Read your data

```bash
python3 scripts/analyze.py               # -> reports/latest.md (since DATA_SINCE)
python3 scripts/analyze.py --months 4    # -> reports/report-<date>-4mo.md (recent window)
python3 scripts/progress.py              # -> reports/progress.html (dashboard)
python3 scripts/story.py                 # -> reports/story.html (story since July)
open reports/story.html
```

How to read the dashboard (`progress.html`):

- **Volume mingguan** — bars are km/week; the line is the 4-week rolling
  average. Ramp rate is your injury-risk dial.
- **Tren pace** — every dot is one run (higher = faster); the red line is the
  5-run rolling median. This is your fitness curve.
- **Efisiensi jantung** — average HR vs pace, pale = older, dark = recent.
  Points drifting *down* at the same pace means real aerobic gains.
- **Kalender konsistensi** — weeks × days; `L` marks long runs (≥ 8 km).
- The **insights list** at the top is computed from your data (pace delta,
  HR at matched pace, ramp rate, consistency %) — not hand-written.

What the story page (`story.html`) shows. Every headline and sentence on it is
computed from the data, so it rewrites itself on each sync:

| Section | What it answers |
|---|---|
| Angka yang jujur | Totals after merging duplicates, and how far the phone and watch copies of the same run disagree |
| Lari vs jalan | Each run as a timeline of running vs walking/stopped (10-second slices slower than 12:00/km) |
| Jantung yang sama | Pace while HR is 135–145 bpm, above weekly volume. Skips the first 5 minutes (HR lags pace) and needs ≥ 5 minutes in the band |
| Intensitas | Share of time per HR band, per run |
| Di mana | Tracks and start places, month by month (places matched within 1.5 km of a run's first GPS point) |
| Kapan | Weekday × week calendar and start-hour histogram |
| Lari terjauh | km splits, HR and elevation on one distance axis, with a hover crosshair |
| Beban latihan | This week vs the 4 weeks before (acute:chronic ratio), with the km that brings next week back to ≤ 1.3× |

The page works in light and dark mode and at phone width; every chart has a
hover tooltip and a "Lihat tabel" table view.

### 5. Run the tests

```bash
python3 -m unittest discover -s tests
```

They cover the data scope, local-time decoding, duplicate merging, the FIT
reader (on a synthetic Huawei-style file, plus truncated and multi-session
files), the watch-over-Strava distance rule, download retries, km splits,
short/partial tracks, sheet formula handling, and `sync.sh`'s exit status.

### 6. Schedule it

A macOS LaunchAgent runs the full pipeline (fetch → analyze → progress →
story → sheet) **weekly, Sunday 22:00**. Set it up on the one machine that should
sync. `scripts/sync.sh` calls `/opt/homebrew/bin/python3`; if `which python3`
says otherwise on that machine, edit `PY` in `sync.sh` first.

```bash
cat > ~/Library/LaunchAgents/com.ihsan.strava-sync.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.ihsan.strava-sync</string>
    <key>ProgramArguments</key>
    <array><string>/absolute/path/to/strava-reader/scripts/sync.sh</string></array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>0</integer>
        <key>Hour</key><integer>22</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardErrorPath</key>
    <string>/absolute/path/to/strava-reader/logs/launchd.err</string>
</dict>
</plist>
PLIST
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ihsan.strava-sync.plist
```

- Manual run now: `launchctl kickstart gui/$(id -u)/com.ihsan.strava-sync`
  (or just `scripts/sync.sh`)
- Logs: `logs/cron.log`; the last line lists every stage's exit status
  (`fetch=0 analyze=0 …`), and `sync.sh` exits 1 if any stage failed (each
  stage still runs after an earlier one fails)
- Change the schedule: edit `StartCalendarInterval` (`Weekday 0` = Sunday),
  then `launchctl bootout` + `bootstrap` again.
- Pause it on this machine (survives reboots, keeps the plist):
  `launchctl bootout gui/$(id -u)/com.ihsan.strava-sync && launchctl disable gui/$(id -u)/com.ihsan.strava-sync`.
  Resume: `launchctl enable gui/$(id -u)/com.ihsan.strava-sync && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ihsan.strava-sync.plist`
- Remove: `launchctl bootout gui/$(id -u)/com.ihsan.strava-sync && rm ~/Library/LaunchAgents/com.ihsan.strava-sync.plist`
- `fetch.py` leaves its headless Chrome running (CDP on `127.0.0.1:9222`) for
  the next sync. On a machine that no longer syncs, stop it:
  `pkill -f -- "--user-data-dir=$PWD/chrome-data"`

### 7. Mirror to a Google Sheet (optional)

`scripts/sheet.py` rebuilds six tabs from `data/` (Aktivitas, Data alat
(FIT), Analisis lari, Split per km, Mingguan, Catatan) and writes them to
`reports/sheet.json`. With `--push` it POSTs them to a small Apps Script web
app bound to the sheet, which **rewrites each tab whole**: every tab is first
written to a hidden staging tab and only then copied over the live one, so a
push that fails mid-way leaves the previous contents; pushes run one at a
time. No row is ever appended, so rerunning a sync cannot create
duplicates. Text is always written as text (an activity named `=1+1` stays
`=1+1`); the only formulas are the `link` column's. `sync.sh` runs it with
`--push` after the story page.

One-time setup:

1. Put a random token in `.env` (ignored):
   `STRAVA_SHEET_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")`
   and the sheet's own id (the long id in its URL) as `STRAVA_SHEET_ID=...`.
2. In the sheet: **Extensions → Apps Script**, paste `sheets/webapp.gs`, then set
   the script properties (**Project Settings → Script Properties**) `TOKEN` to
   the same token and `SPREADSHEET_ID` to the same sheet id; the web app
   writes only to that sheet and refuses a push for any other id.
3. **Deploy → New deployment → Web app**, execute as me, access Anyone.
   Authorize, copy the `/exec` URL into `.env` as `STRAVA_SHEET_URL=...`. After
   any later edit of `webapp.gs`, re-deploy with **Version: New version** —
   `/exec` keeps running its pinned old code otherwise, and `--push` fails on
   the version stamp (`v`) instead of writing wrong data.
4. Test: `python3 scripts/sheet.py --push`.

Tab "Analisis lari" uses `data/places.json` for the `tempat` column, like
`story.html`.

## Known limitations

- **Totals won't match the Strava app exactly.** The reports use the watch's
  own distance (see "How the data is cleaned"), which for runs where the watch
  GPS stalled is longer than what Strava shows.
- **The FIT reader is minimal.** It reads only what the reports need (file id,
  session totals, speed records), and knows that Huawei writes its whole 5 s
  speed series up front under a few timestamps. Another device's quirks may
  need a look at `scripts/fit.py`.
- **HR bands are rough.** They are anchored to the highest HR ever recorded,
  not a measured max HR.
- **Places are yours to name.** `story.py` matches each run's first GPS point
  to `data/places.json` (`[["Name", lat, lon], ...]`, within 1.5 km). The file
  lives in the ignored `data/` because it maps your routine; without it every
  run is "lainnya".

## Layout

```
scripts/config.py     # DATA_SINCE (2026-01-01) + local-time decoding, shared by all scripts
scripts/fetch.py      # sync: session + summaries + FIT/GPX downloads (since DATA_SINCE)
scripts/fit.py        # minimal FIT reader: device, session totals, speed series
scripts/analyze.py    # markdown report (since DATA_SINCE or --months N), watch distance, duplicate merging
scripts/progress.py   # progress.html dashboard (runs since June 2026)
scripts/story.py      # story.html, GPX point-by-point story (runs since July 2026)
scripts/sheet.py      # Google Sheet mirror: rebuilds every tab, rewrites it whole (--push)
sheets/webapp.gs      # Apps Script receiver bound to the sheet
scripts/sync.sh       # pipeline wrapper for the scheduler
scripts/relogin.sh    # visible Chrome for the one manual login
tests/                # python3 -m unittest discover -s tests
chrome-data/          # automation Chrome profile (your Strava session)  [ignored]
data/                 # your data                                          [ignored]
reports/              # generated reports                                  [ignored]
logs/                 # scheduler output                                   [ignored]
diagrams/             # mechanism diagrams (public-safe)
```

## Troubleshooting

- **`SESSION EXPIRED`** (fetch exits 2, writes `data/SESSION_EXPIRED`):
  run `scripts/relogin.sh`, log in once, re-run. Nothing else to fix.
- **`STRAVA ERROR`** (fetch exits 1): Strava answered with a server error or
  an error page instead of data. Not a login problem; the next sync retries.
- **`push failed: spreadsheetId … is not the configured sheet`**: the
  `SPREADSHEET_ID` script property and `STRAVA_SHEET_ID` in `.env` disagree.
- **Cloudflare block**: `fetch.py` automatically retries the request inside
  the logged-in browser (a real Chrome fetch) — no action needed.
- **`crontab -` hangs**: on some macOS setups writing crontabs stalls; that's
  why this repo uses launchd.
- **Homebrew Python `pyexpat` broken** (`ImportError: No module named expat`):
  the scripts parse GPX with regex on purpose; you shouldn't hit this.

## Safety & privacy

- `chrome-data/` holds a live Strava session — **treat it like a password**.
  So does `data/session.json` (the synced cookie).
- `data/` is your location history. `reports/` is your training log, and
  `story.html` draws your routes and start points, which can reveal where you
  live — don't share it without cropping the start and end of each route.
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
