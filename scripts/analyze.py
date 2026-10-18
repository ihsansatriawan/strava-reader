#!/usr/bin/env python3
"""Analyze synced Strava data and write a markdown report.

Reads data/activities.json (summaries) and data/gpx/*.gpx (GPS + heart rate).
Writes reports/report-YYYY-MM-DD.md and refreshes reports/latest.md.
Stdlib only.
"""
import calendar
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fit  # noqa: E402
from config import DATA_SINCE, in_scope, local_start  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
GPX_DIR = DATA / "gpx"
FIT_DIR = DATA / "fit"
MOVING_SPEED = {"Run": 1.0}  # m/s at or above which the device counts as moving; else 0.5
REPORTS = ROOT / "reports"
ACTIVITIES_FILE = DATA / "activities.json"
EXPIRED_FILE = DATA / "SESSION_EXPIRED"


def hms(seconds):
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def pace_str(seconds, meters):
    if not meters:
        return "-"
    sec_per_km = seconds / (meters / 1000)
    m, s = int(sec_per_km // 60), int(round(sec_per_km % 60))
    return f"{m}:{s:02d} /km"


TRKPT_RE = re.compile(r"<trkpt\b")
HR_RE = re.compile(r"<gpxtpx:hr>(\d+)</gpxtpx:hr>")


def gpx_stats(path):
    """Return (points, avg_hr, max_hr) parsed from a Strava GPX.

    Regex-based on purpose: Homebrew Python's pyexpat can be broken against
    the system libexpat, and Strava GPX is machine-generated and regular.
    """
    text = path.read_text(errors="replace")
    hrs = [int(v) for v in HR_RE.findall(text)]
    return {"points": len(TRKPT_RE.findall(text)),
            "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
            "max_hr": max(hrs) if hrs else None}


def gpx_has_hr(path):
    return path.exists() and HR_RE.search(path.read_text(errors="replace")) is not None


def use_device(a, fs):
    """Prefer the recording device's own distance and moving time over Strava's GPS recount.

    Strava's number stays in strava_km; the FIT summary rides along as device.
    A recording made by Strava's phone app is left alone (same numbers).
    """
    if not fs or fs["manufacturer"] == fit.STRAVA_APP or not fs["km"]:
        return a
    a = dict(a, strava_km=a["km"], km=fs["km"], device=fs)
    if fs["speed"]:
        a["moving_s"] = fit.moving_seconds(fs, MOVING_SPEED.get(a["type"], 0.5))
    return a


DUP_WINDOW_S = 180


def dedupe(acts, has_hr):
    """Collapse one session uploaded twice (phone app + watch sync).

    Same sport starting within DUP_WINDOW_S counts as one session. The copy
    with heart rate wins, then the earlier upload. Returns (kept, n_merged),
    kept in the input order.
    """
    groups = []
    for a in sorted(acts, key=lambda a: a["start"]):
        g = groups[-1] if groups else None
        if g and g[0]["type"] == a["type"] and (a["start"] - g[0]["start"]).total_seconds() < DUP_WINDOW_S:
            g.append(a)
        else:
            groups.append([a])
    keep = set()
    for g in groups:
        if len(g) == 1:
            keep.add(id(g[0]))
        else:
            keep.add(id(min(g, key=lambda a: (not has_hr(a), int(a["id"])))))
    return [a for a in acts if id(a) in keep], len(acts) - len(keep)


def read_activities():
    """Return (fetched_at, every activity since DATA_SINCE newest first) - duplicates included.

    Distance and moving time come from the recording device's FIT file when
    there is one (see use_device); otherwise from Strava's summary.
    """
    raw = json.loads(ACTIVITIES_FILE.read_text())
    acts = []
    for m in in_scope(raw["models"]):
        a = {
            "id": m["id_str"], "name": m["name"], "type": m["sport_type"],
            "start": local_start(m["start_date_local_raw"]),
            "km": m["distance_raw"] / 1000,
            "moving_s": m["moving_time_raw"],
            "elapsed_s": m["elapsed_time_raw"],
            "elev": m["elevation_gain_raw"],
            "commute": m["commute"], "trainer": m["trainer"],
        }
        f = FIT_DIR / f"{a['id']}.fit"
        acts.append(use_device(a, fit.summary(f) if f.exists() else None))
    acts.sort(key=lambda a: a["start"], reverse=True)
    return raw.get("fetched_at", "?"), acts


def load():
    """Return (fetched_at, activities newest first, n duplicate uploads merged)."""
    fetched_at, acts = read_activities()
    acts, merged = dedupe(acts, has_hr=lambda a: gpx_has_hr(GPX_DIR / f"{a['id']}.gpx"))
    return fetched_at, acts, merged


def iso_weeks_back(now, n):
    monday = now.date() - timedelta(days=now.isoweekday() - 1)
    weeks = []
    for i in range(n - 1, -1, -1):
        wk = monday - timedelta(weeks=i)
        iso = wk.isocalendar()
        weeks.append((iso[0], iso[1], wk))
    return weeks


def week_key(d):
    iso = d.date().isocalendar()
    return (iso[0], iso[1])



def months_ago(now, n):
    month, year = now.month, now.year
    for _ in range(n):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, now.hour, now.minute)

def current_streak(acts):
    days = {a["start"].date() for a in acts}
    today = datetime.now().date()
    start = today if today in days else today - timedelta(days=1)
    streak = 0
    d = start
    while d in days:
        streak += 1
        d -= timedelta(days=1)
    return streak


def build_report(fetched_at, acts, scope=f"since {DATA_SINCE:%Y-%m-%d}", merged=0):
    now = datetime.now()
    L = []
    L.append(f"# Strava training report - {now:%Y-%m-%d} ({scope})")
    L.append(f"_Data synced: {fetched_at}, {len(acts)} activities, window: {scope}"
             + (f", {merged} duplicate uploads merged (phone + watch)" if merged else "") + "_")
    if EXPIRED_FILE.exists():
        L.append("\n> WARNING: last fetch hit an expired session - run scripts/relogin.sh and re-fetch.")
    L.append("")

    # --- All-time totals by sport ---
    by_sport = defaultdict(lambda: {"n": 0, "km": 0.0, "s": 0, "elev": 0.0})
    for a in acts:
        s = by_sport[a["type"]]
        s["n"] += 1
        s["km"] += a["km"]
        s["s"] += a["moving_s"]
        s["elev"] += a["elev"]
    L.append(f"## Totals by sport ({scope})")
    L.append("| Sport | Activities | Distance | Moving time | Elevation |")
    L.append("|---|---:|---:|---:|---:|")
    for t, s in sorted(by_sport.items(), key=lambda kv: -kv[1]["km"]):
        L.append(f"| {t} | {s['n']} | {s['km']:.1f} km | {hms(s['s'])} | {s['elev']:.0f} m |")
    L.append("")

    # --- This week vs previous 4 weeks ---
    this_wk = week_key(now)
    prev_keys = [week_key(now - timedelta(weeks=i)) for i in range(1, 5)]
    wk_km = sum(a["km"] for a in acts if week_key(a["start"]) == this_wk)
    wk_s = sum(a["moving_s"] for a in acts if week_key(a["start"]) == this_wk)
    p_km = sum(a["km"] for a in acts if week_key(a["start"]) in prev_keys) / 4
    p_s = sum(a["moving_s"] for a in acts if week_key(a["start"]) in prev_keys) / 4
    d_km = ((wk_km / p_km - 1) * 100) if p_km else float("nan")
    d_s = ((wk_s / p_s - 1) * 100) if p_s else float("nan")
    L.append("## This week vs 4-week average")
    L.append("| Metric | This week | 4-week avg | Delta |")
    L.append("|---|---:|---:|---:|")
    L.append(f"| Distance | {wk_km:.1f} km | {p_km:.1f} km | {d_km:+.0f}% |")
    L.append(f"| Moving time | {hms(wk_s)} | {hms(p_s)} | {d_s:+.0f}% |")
    L.append("")

    # --- Last 12 weeks table ---
    L.append("## Last 12 weeks")
    L.append("| Week (Mon) | Activities | Distance | Moving time | Elevation |")
    L.append("|---|---:|---:|---:|---:|")
    for year, week, wk_monday in iso_weeks_back(now, 12):
        wk_acts = [a for a in acts if week_key(a["start"]) == (year, week)]
        km = sum(a["km"] for a in wk_acts)
        s = sum(a["moving_s"] for a in wk_acts)
        elev = sum(a["elev"] for a in wk_acts)
        marker = " **(now)**" if week_key(now) == (year, week) else ""
        L.append(f"| {wk_monday:%Y-%m-%d}{marker} | {len(wk_acts)} | {km:.1f} km | {hms(s)} | {elev:.0f} m |")
    L.append("")

    # --- Run pace trend (monthly, distance-weighted) ---
    runs = [a for a in acts if a["type"] == "Run" and a["km"] >= 1 and not a["trainer"]]
    monthly = defaultdict(lambda: {"km": 0.0, "s": 0, "n": 0})
    for a in runs:
        k = (a["start"].year, a["start"].month)
        monthly[k]["km"] += a["km"]
        monthly[k]["s"] += a["moving_s"]
        monthly[k]["n"] += 1
    if monthly:
        L.append("## Run pace trend (monthly)")
        L.append("| Month | Runs | Distance | Avg pace |")
        L.append("|---|---:|---:|---:|")
        for k in sorted(monthly)[-6:]:
            m = monthly[k]
            L.append(f"| {k[0]}-{k[1]:02d} | {m['n']} | {m['km']:.1f} km | {pace_str(m['s'], m['km'] * 1000)} |")
        L.append("")

    # --- Recent runs with heart rate (from GPX) ---
    hr_rows = []
    for a in runs[:30]:
        g = GPX_DIR / f"{a['id']}.gpx"
        if not g.exists():
            continue
        try:
            st = gpx_stats(g)
        except OSError:
            continue
        if st["avg_hr"]:
            hr_rows.append((a, st))
        if len(hr_rows) >= 10:
            break
    if hr_rows:
        L.append("## Recent runs with heart rate")
        L.append("| Date | Name | Distance | Pace | Avg HR | Max HR |")
        L.append("|---|---|---:|---:|---:|---:|")
        for a, st in hr_rows:
            L.append(f"| {a['start']:%Y-%m-%d} | {a['name']} | {a['km']:.1f} km | "
                     f"{pace_str(a['moving_s'], a['km'] * 1000)} | {st['avg_hr']} | {st['max_hr']} |")
        L.append("")

    # --- Records ---
    L.append("## Records")
    L.append("| Record | Value |")
    L.append("|---|---|")
    for t in sorted(by_sport):
        best = max((a for a in acts if a["type"] == t), key=lambda a: a["km"])
        L.append(f"| Longest {t} | {best['km']:.1f} km - {best['name']} ({best['start']:%Y-%m-%d}) |")
    if acts:
        most_elev = max(acts, key=lambda a: a["elev"])
        L.append(f"| Most elevation | {most_elev['elev']:.0f} m - {most_elev['name']} ({most_elev['start']:%Y-%m-%d}) |")
        longest_time = max(acts, key=lambda a: a["moving_s"])
        L.append(f"| Longest moving time | {hms(longest_time['moving_s'])} - {longest_time['name']} ({longest_time['start']:%Y-%m-%d}) |")
    L.append(f"| Current streak | {current_streak(acts)} day(s) |")
    L.append("")

    # --- Last 15 activities ---
    L.append("## Last 15 activities")
    L.append("| Date | Name | Sport | Distance | Moving time | Elevation |")
    L.append("|---|---|---|---:|---:|---:|")
    for a in acts[:15]:
        L.append(f"| {a['start']:%Y-%m-%d} | {a['name']} | {a['type']} | "
                 f"{a['km']:.1f} km | {hms(a['moving_s'])} | {a['elev']:.0f} m |")
    L.append("")
    return "\n".join(L)


def main():
    if not ACTIVITIES_FILE.exists():
        raise SystemExit("no data yet - run scripts/fetch.py first")
    REPORTS.mkdir(parents=True, exist_ok=True)
    fetched_at, acts, merged = load()
    if not acts:
        raise SystemExit("activities list is empty")
    months = None
    if len(sys.argv) == 3 and sys.argv[1] == "--months":
        months = int(sys.argv[2])
    if months:
        cutoff = months_ago(datetime.now(), months)
        acts = [a for a in acts if a["start"] >= cutoff]
        if not acts:
            raise SystemExit(f"no activities in the last {months} months")
        scope = f"last {months} months (since {cutoff:%Y-%m-%d})"
        report = build_report(fetched_at, acts, scope)
        out = REPORTS / f"report-{datetime.now():%Y-%m-%d}-{months}mo.md"
        out.write_text(report)
    else:
        report = build_report(fetched_at, acts, merged=merged)
        out = REPORTS / f"report-{datetime.now():%Y-%m-%d}.md"
        out.write_text(report)
        (REPORTS / "latest.md").write_text(report)
    print(f"report written: {out}")
    runs = [a for a in acts if a["type"] == "Run" and a["km"] >= 1]
    week_km = sum(a["km"] for a in acts if week_key(a["start"]) == week_key(datetime.now()))
    print(f"summary: {len(acts)} activities, {len(runs)} runs, "
          f"this week {week_km:.1f} km, streak {current_streak(acts)} day(s)")


if __name__ == "__main__":
    main()
