#!/usr/bin/env python3
"""Build reports/story.html - the story of every run since SINCE.

Unlike progress.html (a rolling dashboard of summaries), this page reads each
GPX track point by point:
  1. Honest totals   - phone + watch duplicates merged; how far the two disagree
  2. Run / walk      - every run as a timeline of running vs walking or stopped
  3. Same heart      - pace while HR is 135-145 bpm, above weekly volume
  4. Intensity mix   - share of time per HR band, per run
  5. Where           - tracks and start places, month by month
  6. When            - weekday x week calendar + start hour
  7. Longest run     - km splits, HR and elevation on one distance axis
  8. Load            - this week vs the 4 weeks before
Every number and sentence on the page is computed here. Stdlib only.
"""
import bisect
import html
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fit  # noqa: E402
from analyze import GPX_DIR, dedupe, gpx_has_hr, read_activities  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
SINCE = datetime(2026, 7, 1)

WALK_SPEED = 1000 / 720          # m/s; slower than 12:00 /km = walking or stopped
HR_LO, HR_HI = 135, 145          # the "same heart" band for the pace comparison
WARMUP_S = 300                   # HR lags pace early on: skip it, or the band reads too fast
MIN_BAND_S = 300                 # a run needs this long in the band to count
GPS_TRUST = 0.03                 # GPS distance within 3% of the device's own total: use the GPS
BANDS = [(0, 125, "< 125"), (125, 140, "125–139"), (140, 155, "140–154"),
         (155, 165, "155–164"), (165, 999, "≥ 165")]
HARD_HR = 155                    # "fast" = time at or above this
# Start places, matched within PLACE_RADIUS_M of a run's first GPS point. They
# map your routine, so they live outside the repo in data/places.json:
# [["Name", lat, lon], ...]. Without the file every run is OTHER.
PLACES_FILE = ROOT / "data" / "places.json"
PLACES = [tuple(p) for p in json.loads(PLACES_FILE.read_text())] if PLACES_FILE.exists() else []
PLACE_RADIUS_M = 1500
OTHER = "lainnya"
# Acute:chronic load zones (common rule of thumb, not a law).
LOAD_ZONES = [(0, 0.8, "rendah", "m-low", "↓"), (0.8, 1.3, "aman", "m-ok", "✓"),
              (1.3, 1.5, "waspada", "m-warn", "!"), (1.5, 3.0, "tinggi", "m-crit", "▲")]

BULAN = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
BULAN_PANJANG = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
                 "Agustus", "September", "Oktober", "November", "Desember"]
HARI = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
HARI_PANJANG = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

W = 960


# ---------------- formatting ----------------

def num(x, d=1):
    return f"{x:,.{d}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def mmss(sec):
    s = int(round(sec))
    return f"{s // 60}:{s % 60:02d}"


def secs(sec):
    """A pace difference in words: '35 detik', '1:05 menit'."""
    return f"{int(round(sec))} detik" if round(sec) < 60 else f"{mmss(sec)} menit"


def tgl(d):
    return f"{d.day} {BULAN[d.month]}"


def esc(s):
    return html.escape(str(s), quote=True)


def month_range(ms):
    """[7, 8, 9] -> 'Jul–Sep'."""
    return BULAN[ms[0]] if len(ms) == 1 else f"{BULAN[ms[0]]}–{BULAN[ms[-1]]}"


# ---------------- GPX ----------------

PT_RE = re.compile(r'<trkpt lat="([-\d.]+)" lon="([-\d.]+)">(.*?)</trkpt>', re.S)
TIME_RE = re.compile(r"<time>([^<]+)</time>")
ELE_RE = re.compile(r"<ele>([-\d.]+)</ele>")
HR_RE = re.compile(r"<gpxtpx:hr>(\d+)</gpxtpx:hr>")


def hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def track(path, device=None):
    """(GPX points, whether d was re-timed on the device) - points carry
    lat, lon, ele, hr, d (cumulative m), s (seconds from start).

    d follows the GPS points. When the recording device's FIT summary is given
    and the GPS distance misses the device's own total by more than GPS_TRUST
    (the watch GPS stalled), d follows the device's speed series instead. That
    series is an estimate too: right totals, but single km can be off by a
    minute, so per-km bests from it aren't trusted.
    Regex-based for the same reason as analyze.gpx_stats.
    """
    pts, d, t0 = [], 0.0, None
    for m in PT_RE.finditer(path.read_text(errors="replace")):
        body = m[3]
        lat, lon = float(m[1]), float(m[2])
        t = datetime.fromisoformat(TIME_RE.search(body)[1].replace("Z", "+00:00"))
        if pts:
            d += hav(pts[-1]["lat"], pts[-1]["lon"], lat, lon)
        else:
            t0 = t
        ele, hr = ELE_RE.search(body), HR_RE.search(body)
        pts.append({"lat": lat, "lon": lon, "d": d, "s": (t - t0).total_seconds(),
                    "ele": float(ele[1]) if ele else None, "hr": int(hr[1]) if hr else None})
    retimed = bool(pts and device and device["speed"] and device["km"]
                   and abs(d / (device["km"] * 1000) - 1) > GPS_TRUST)
    if retimed:
        # The device clock skips pauses: GPX gaps longer than a sample don't
        # advance it, and any remaining drift is spread over the run.
        active, clock = [], 0.0
        for a, b in zip([pts[0]] + pts, pts):
            clock += min(b["s"] - a["s"], fit.MAX_SAMPLE_S)
            active.append(clock)
        scale = device["elapsed_s"] / clock if clock else 1
        curve = fit.distance_curve(device)
        for p, c in zip(pts, active):
            p["d"] = along(curve, c * scale)
    return pts, retimed


def along(curve, t):
    """Cumulative metres at t seconds, linear between the curve's samples."""
    k = bisect.bisect_right(curve, (t, float("inf")))
    if k == 0:
        return curve[0][1]
    if k == len(curve):
        return curve[-1][1]
    (t0, d0), (t1, d1) = curve[k - 1], curve[k]
    return d0 + (d1 - d0) * (t - t0) / (t1 - t0) if t1 > t0 else d1


def slices(pts, sec=10):
    """Back-to-back ~10 s slices of a track: start, duration, meters, mean HR."""
    out, i = [], 0
    while i < len(pts) - 1:
        j = i + 1
        while j < len(pts) - 1 and pts[j]["s"] - pts[i]["s"] < sec:
            j += 1
        dur = pts[j]["s"] - pts[i]["s"]
        if dur > 0:
            hrs = [p["hr"] for p in pts[i:j + 1] if p["hr"]]
            out.append({"t": pts[i]["s"], "dur": dur, "m": pts[j]["d"] - pts[i]["d"],
                        "d": pts[i]["d"], "hr": statistics.mean(hrs) if hrs else None})
        i = j
    return out


def place_of(lat, lon):
    if not PLACES:
        return OTHER
    best = min(PLACES, key=lambda p: hav(lat, lon, p[1], p[2]))
    return best[0] if hav(lat, lon, best[1], best[2]) <= PLACE_RADIUS_M else OTHER


def analyse(a):
    """The run's track read point by point, or None when the GPX holds no
    usable track (fewer than two points, no distance or no time) - the
    activity's summary still counts in the totals."""
    pts, retimed = track(GPX_DIR / f"{a['id']}.gpx", a.get("device"))
    if len(pts) < 2 or pts[-1]["d"] <= 0 or pts[-1]["s"] <= 0:
        return None
    sl = slices(pts)
    total = sum(s["dur"] for s in sl)
    moving = [s for s in sl if s["m"] / s["dur"] >= WALK_SPEED]
    stops = []
    for s in sl:
        if s["m"] / s["dur"] < WALK_SPEED:
            if stops and abs(stops[-1][1] - s["t"]) < 0.5:
                stops[-1][1] = s["t"] + s["dur"]
            else:
                stops.append([s["t"], s["t"] + s["dur"]])
    hr_sl = [s for s in sl if s["hr"]]
    band = [s for s in moving if s["t"] >= WARMUP_S and s["hr"] and HR_LO <= s["hr"] <= HR_HI]
    band_s = sum(s["dur"] for s in band)
    hrs = [p["hr"] for p in pts if p["hr"]]
    return {**a, "pts": pts, "slices": sl, "total_s": total, "dist": pts[-1]["d"],
            "walk_pct": 100 * (1 - sum(s["dur"] for s in moving) / total) if total else 0.0,
            "stops": stops, "has_hr": bool(hrs),
            "avg_hr": statistics.mean(hrs) if hrs else None,
            "pace_hr": band_s / (sum(s["m"] for s in band) / 1000) if band_s >= MIN_BAND_S else None,
            "zones": [sum(s["dur"] for s in hr_sl if lo <= s["hr"] < hi) for lo, hi, _ in BANDS],
            "place": place_of(pts[0]["lat"], pts[0]["lon"]), "retimed": retimed}


def splits(pts):
    """Full kilometres: elapsed seconds, mean HR, metres climbed.

    Each kilometre ends exactly where the track crosses it, interpolated
    between the samples on either side (several kilometres can fall between
    two samples). HR is the mean of the real samples inside that km."""
    def cut(a, b, d):
        f = (d - a["d"]) / (b["d"] - a["d"])
        ele = (a["ele"] + f * (b["ele"] - a["ele"])
               if a["ele"] is not None and b["ele"] is not None else None)
        return {"d": d, "s": a["s"] + f * (b["s"] - a["s"]), "ele": ele, "hr": None}

    def close(seg):
        hrs = [q["hr"] for q in seg if q["hr"]]
        gain = sum(max(0.0, seg[i]["ele"] - seg[i - 1]["ele"]) for i in range(1, len(seg))
                   if seg[i]["ele"] is not None and seg[i - 1]["ele"] is not None)
        return {"km": k, "s": seg[-1]["s"] - seg[0]["s"],
                "hr": statistics.mean(hrs) if hrs else None, "gain": gain}

    out, k, seg = [], 1, pts[:1]
    for a, b in zip(pts, pts[1:]):
        while b["d"] > a["d"] and b["d"] >= k * 1000:
            x = b if b["d"] == k * 1000 else cut(a, b, k * 1000)  # a sample on the mark is the mark
            out.append(close(seg + [x]))
            k, seg = k + 1, [x]
        if seg[-1] is not b:
            seg.append(b)
    return out


def monday(d):
    d = d.date() if isinstance(d, datetime) else d
    return d - timedelta(days=d.weekday())


# ---------------- svg helpers ----------------

def svg(w, h, body, label, extra=""):
    return (f'<svg viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="{esc(label)}"{extra}>'
            f"{body}</svg>")


def T(x, y, s, cls="ax", anchor="start"):
    return f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" text-anchor="{anchor}">{esc(s)}</text>'


def tip(*lines):
    """Hover/focus readout; first line is the value, the rest label it."""
    return f' data-tip="{esc(chr(10).join(lines))}" tabindex="0"'


def hline(x1, x2, y, cls="grid"):
    return f'<line class="{cls}" x1="{x1:.1f}" x2="{x2:.1f}" y1="{y:.1f}" y2="{y:.1f}"/>'


def vline(x, y1, y2, cls="grid"):
    return f'<line class="{cls}" x1="{x:.1f}" x2="{x:.1f}" y1="{y1:.1f}" y2="{y2:.1f}"/>'


def dot(x, y, cls, r=4.5):
    return f'<circle class="dot {cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r}"/>'


def col(x, y, w, h, cls):
    """Column with a 4px rounded data-end, square at the baseline."""
    r = min(4, h, w / 2)
    return (f'<path class="{cls}" d="M{x:.1f},{y + h:.1f} L{x:.1f},{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
            f'L{x + w - r:.1f},{y:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} L{x + w:.1f},{y + h:.1f} Z"/>')


def legend(x, y, items):
    """items: (kind, cls, label) with kind 'dot' | 'rect' | 'line'."""
    out = []
    for kind, cls, label in items:
        if kind == "dot":
            out.append(f'<circle class="{cls}" cx="{x + 5}" cy="{y - 4}" r="4.5"/>')
        elif kind == "rect":
            out.append(f'<rect class="{cls}" x="{x}" y="{y - 9}" width="10" height="10" rx="2"/>')
        else:
            out.append(f'<line class="{cls}" x1="{x}" x2="{x + 14}" y1="{y - 4}" y2="{y - 4}"/>')
        out.append(T(x + 18, y, label, "lg"))
        x += 18 + len(label) * 6.3 + 22
    return "".join(out)


def table(head, rows, summary="Lihat tabel"):
    """head: labels; a label starting with '#' is a numeric (right-aligned) column."""
    nums = [h.startswith("#") for h in head]
    th = "".join(f'<th class="n">{esc(h[1:])}</th>' if n else f"<th>{esc(h)}</th>" for h, n in zip(head, nums))
    body = "".join("<tr>" + "".join(f'<td class="n">{esc(c)}</td>' if n else f"<td>{esc(c)}</td>"
                                    for c, n in zip(r, nums)) + "</tr>" for r in rows)
    return f'<details><summary>{esc(summary)}</summary><table><tr>{th}</tr>{body}</table></details>'


# ---------------- 1. honest totals ----------------

def chart_twins(pairs, labels):
    X0, X1, top, rh = 84, 860, 40, 22
    kmax = math.ceil(max(max(k["km"], d["km"]) for k, d in pairs) / 2) * 2
    H = top + rh * len(pairs) + 28

    def X(km):
        return X0 + km / kmax * (X1 - X0)

    b = [legend(X0, 16, [("dot", "dot s-kept", labels[0]), ("dot", "dot s-drop", labels[1])])]
    for k in range(0, kmax + 1, 2):
        b.append(vline(X(k), top - 4, H - 24))
        b.append(T(X(k), H - 8, f"{k} km", anchor="middle"))
    for i, (k, d) in enumerate(pairs):
        y = top + i * rh + rh / 2
        diff = (d["km"] / k["km"] - 1) * 100
        b.append(T(X0 - 12, y + 4, tgl(k["start"]), anchor="end"))
        b.append(hline(X(k["km"]), X(d["km"]), y, "link"))
        b.append(dot(X(k["km"]), y, "s-kept") + dot(X(d["km"]), y, "s-drop"))
        b.append(T(X1 + 14, y + 4, f"{diff:+.0f}%"))
        b.append(f'<rect class="hit" x="0" y="{y - rh / 2:.1f}" width="{W}" height="{rh}"'
                 + tip(f"{diff:+.0f}% selisih", f"{tgl(k['start'])} · {labels[0]} {num(k['km'], 2)} km",
                       f"{labels[1]} {num(d['km'], 2)} km") + "/>")
    return svg(W, H, "".join(b), "Jarak versi dipakai vs duplikat per lari")


# ---------------- 2. run / walk strip ----------------

def chart_strip(runs, milestone):
    X0, X1, top, rh, bh = 84, 860, 40, 18, 10
    tmax = math.ceil(max(r["total_s"] for r in runs) / 600) * 10  # minutes
    H = top + rh * len(runs) + 28

    def X(sec):
        return X0 + sec / 60 / tmax * (X1 - X0)

    b = [legend(X0, 16, [("rect", "run", "lari"), ("rect", "stop", "jalan / berhenti")])]
    for m in range(0, tmax + 1, 10):
        b.append(vline(X(m * 60), top - 4, H - 24))
        b.append(T(X(m * 60), H - 8, f"{m}" + (" mnt" if m == tmax else ""), anchor="middle"))
    for i, r in enumerate(runs):
        y = top + i * rh
        yb = y + (rh - bh) / 2
        b.append(T(X0 - 12, yb + 9, tgl(r["start"]), anchor="end"))
        b.append(f'<rect class="run" x="{X0}" y="{yb:.1f}" width="{X(r["total_s"]) - X0:.1f}" height="{bh}" rx="2"/>')
        for t0, t1 in r["stops"]:
            b.append(f'<rect class="stop" x="{X(t0):.1f}" y="{yb:.1f}" width="{max(0.8, X(t1) - X(t0)):.1f}" height="{bh}"/>')
        b.append(T(X1 + 14, yb + 9, f"{r['walk_pct']:.0f}%"))
        if r is milestone and X(r["total_s"]) < X1 - 220:
            b.append(T(X(r["total_s"]) + 10, yb + 9, f"← {num(r['km'])} km pertama tanpa berhenti", "ann"))
        b.append(f'<rect class="hit" x="0" y="{y:.1f}" width="{W}" height="{rh}"'
                 + tip(f"{r['walk_pct']:.0f}% jalan / berhenti",
                       f"{tgl(r['start'])} · {num(r['km'])} km · {r['total_s'] / 60:.0f} menit · {r['place']}",
                       *(["GPS jam macet: dihitung dari kecepatan jam"] if r["retimed"] else [])) + "/>")
    return svg(W, H, "".join(b), "Lari vs jalan atau berhenti, per lari")


# ---------------- 3. same heart: pace @ HR band + weekly volume ----------------

def chart_same_heart(runs, weeks, wk_km, hl_place):
    X0, X1 = 64, 900
    A0, A1, B0, B1 = 44, 236, 290, 410
    t0 = datetime.combine(weeks[0], time())
    t1 = datetime.combine(weeks[-1] + timedelta(days=7), time())

    def X(d):
        return X0 + (d - t0).total_seconds() / (t1 - t0).total_seconds() * (X1 - X0)

    pts = [r for r in runs if r["pace_hr"]]
    pmin, pmax = min(r["pace_hr"] for r in pts), max(r["pace_hr"] for r in pts)
    step = 15 if pmax - pmin <= 120 else 30
    lo, hi = math.floor((pmin - 8) / step) * step, math.ceil((pmax + 8) / step) * step

    def Y(p):  # faster (fewer seconds) = higher
        return A0 + (p - lo) / (hi - lo) * (A1 - A0)

    b = [T(X0, A0 - 24, f"pace saat HR {HR_LO}–{HR_HI} bpm · makin atas makin cepat", "pt"),
         legend(X1 - 330, A0 - 24, [("dot", "dot hl", hl_place), ("dot", "dot ctx", "rute lain"),
                                     ("line", "med", "median bulanan")])]
    for p in range(lo, hi + 1, step):
        b.append(hline(X0, X1, Y(p)))
        b.append(T(X0 - 10, Y(p) + 4, mmss(p), anchor="end"))
    by_month = defaultdict(list)
    for r in pts:
        by_month[(r["start"].year, r["start"].month)].append(r["pace_hr"])
    for (y_, m), ps in sorted(by_month.items()):
        if len(ps) < 2:
            continue
        ms = max(t0, datetime(y_, m, 1))
        me = min(t1, datetime(y_ + (m == 12), m % 12 + 1, 1))
        med = statistics.median(ps)
        b.append(hline(X(ms) + 4, X(me) - 4, Y(med), "med"))
        b.append(T(X(ms) + 6, Y(med) + 16, mmss(med), "val"))
    for hl in (False, True):
        for r in pts:
            if (r["place"] == hl_place) != hl:
                continue
            x, y = X(r["start"]), Y(r["pace_hr"])
            b.append(dot(x, y, "hl" if hl else "ctx"))
            b.append(f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="12"'
                     + tip(f"{mmss(r['pace_hr'])} /km", f"{tgl(r['start'])} · {r['place']} · {num(r['km'])} km",
                           f"HR rata-rata {r['avg_hr']:.0f}") + "/>")
    # weekly volume, same time axis
    top = max(5, math.ceil(max(wk_km.values()) / 5) * 5)

    def YB(km):
        return B1 - km / top * (B1 - B0)

    b.append(T(X0, B0 - 18, "km lari per minggu", "pt"))
    for k in range(0, top + 1, 5):
        b.append(hline(X0, X1, YB(k)))
        b.append(T(X0 - 10, YB(k) + 4, str(k), anchor="end"))
    wpx = (X1 - X0) * 7 / ((t1 - t0).days)
    bw = min(24, wpx - 6)
    peak = max(wk_km, key=wk_km.get)
    for w in weeks:
        km = wk_km[w]
        x = X(datetime.combine(w, time())) + (wpx - bw) / 2
        if km > 0:
            b.append(col(x, YB(km), bw, B1 - YB(km), "bar hi" if w == weeks[-1] else "bar"))
        if km > 0 and w in (weeks[-1], peak):
            b.append(T(x + bw / 2, YB(km) - 6, num(km), "val", "middle"))
        b.append(f'<rect class="hit" x="{x - (wpx - bw) / 2:.1f}" y="{B0}" width="{wpx:.1f}" height="{B1 - B0}"'
                 + tip(f"{num(km)} km", f"minggu {tgl(w)}") + "/>")
    m = datetime(t0.year, t0.month, 1)
    while m < t1:
        if m >= t0:
            b.append(vline(X(m), B1, B1 + 6, "axis"))
            b.append(T(X(m) + 4, B1 + 18, BULAN[m.month]))
        m = datetime(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return svg(W, B1 + 26, "".join(b), f"Pace saat HR {HR_LO}–{HR_HI} dan volume mingguan")


# ---------------- 4. intensity mix ----------------

def chart_mix(runs):
    X0, X1, top, rh, bh = 84, 860, 40, 20, 12
    H = top + rh * len(runs) + 28
    b = [legend(X0, 16, [("rect", f"z{i + 1}", lbl + (" bpm" if i == len(BANDS) - 1 else ""))
                         for i, (_, _, lbl) in enumerate(BANDS)])]
    for p in range(0, 101, 25):
        x = X0 + p / 100 * (X1 - X0)
        b.append(vline(x, top - 4, H - 24))
        b.append(T(x, H - 8, f"{p}%", anchor="middle"))
    for i, r in enumerate(runs):
        y = top + i * rh
        yb = y + (rh - bh) / 2
        total = sum(r["zones"]) or 1
        x = X0
        for k, z in enumerate(r["zones"]):
            w = z / total * (X1 - X0)
            if w > 2.5:
                b.append(f'<rect class="z{k + 1}" x="{x + 1:.1f}" y="{yb:.1f}" width="{w - 2:.1f}" height="{bh}" rx="2"/>')
            x += w
        b.append(T(X0 - 12, yb + 10, tgl(r["start"]), anchor="end"))
        b.append(T(X1 + 14, yb + 10, f"{r['avg_hr']:.0f} bpm"))
        hard = sum(z for (lo, _, _), z in zip(BANDS, r["zones"]) if lo >= HARD_HR) / total * 100
        lines = [f"{hard:.0f}% di ≥ {HARD_HR} bpm",
                 f"{tgl(r['start'])} · {r['place']} · {num(r['km'])} km · HR rata-rata {r['avg_hr']:.0f}"]
        lines += [f"{lbl}: {z / total * 100:.0f}%" for (_, _, lbl), z in zip(BANDS, r["zones"])]
        b.append(f'<rect class="hit" x="0" y="{y:.1f}" width="{W}" height="{rh}"' + tip(*lines) + "/>")
    return svg(W, H, "".join(b), "Porsi waktu per rentang detak jantung, per lari")


# ---------------- 5. where ----------------

def chart_map(runs, months):
    allp = [(p["lat"], p["lon"]) for r in runs for p in r["pts"]]
    pad = 0.004
    la0, la1 = min(p[0] for p in allp) - pad, max(p[0] for p in allp) + pad
    lo0, lo1 = min(p[1] for p in allp) - pad, max(p[1] for p in allp) + pad
    kx = math.cos(math.radians((la0 + la1) / 2))
    n, gap, top = len(months), 18, 56
    pw = (W - gap * (n - 1)) / n
    scale = min(pw / ((lo1 - lo0) * kx), 520 / (la1 - la0))
    mw, mh = (lo1 - lo0) * kx * scale, (la1 - la0) * scale
    ox_in = (pw - mw) / 2

    def P(lat, lon):
        return ox_in + (lon - lo0) * kx * scale, (la1 - lat) * scale

    def path(r, step=30):
        out, last = [], None
        for p in r["pts"]:
            if last is None or p["d"] - last >= step:
                x, y = P(p["lat"], p["lon"])
                out.append(f"{'M' if not out else 'L'}{x:.1f},{y:.1f}")
                last = p["d"]
        return "".join(out)

    tracks = {r["id"]: path(r) for r in runs}
    b = ['<defs><g id="ctx-tracks">' + "".join(f'<path class="trk-ctx" d="{d}"/>' for d in tracks.values())
         + "</g></defs>"]
    used = {r["place"] for r in runs} - {OTHER}
    for i, (y_, m) in enumerate(months):
        ox = i * (pw + gap)
        mine = [r for r in runs if (r["start"].year, r["start"].month) == (y_, m)]
        cnt = Counter(r["place"] for r in mine if r["place"] != OTHER)
        b.append(T(ox + 4, 20, BULAN_PANJANG[m], "pt"))
        sub = f"{len(mine)} lari"
        if cnt:
            p, c = cnt.most_common(1)[0]
            sub += f" · {p} {c}×"
        b.append(T(ox + 4, 38, sub))
        b.append(f'<g transform="translate({ox:.1f},{top})">')
        b.append(f'<rect class="panel" x="0" y="0" width="{pw:.1f}" height="{mh:.1f}" rx="4"/>')
        b.append('<use href="#ctx-tracks"/>')
        b.extend(f'<path class="trk" d="{tracks[r["id"]]}"/>' for r in mine)
        for r in mine:
            x, y = P(r["pts"][0]["lat"], r["pts"][0]["lon"])
            b.append(dot(x, y, "start", 3.5))
            b.append(f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="10"'
                     + tip(r["place"], f"{tgl(r['start'])} · {num(r['km'])} km") + "/>")
        for name, la, lo in PLACES:
            if name not in used:
                continue
            x, y = P(la, lo)
            right = x + 8 + len(name) * 6 < pw
            b.append(T(x + 8 if right else x - 8, y + 4, name, "lbl-map", "start" if right else "end"))
        b.append("</g>")
    return svg(W, top + mh + 4, "".join(b), "Rute lari per bulan")


# ---------------- 6. when ----------------

def chart_week(runs, weeks, today):
    CW, G, X0, Y0 = 22, 4, 44, 34
    km = defaultdict(float)
    for r in runs:
        km[r["start"].date()] += r["km"]
    per_day = Counter(r["start"].weekday() for r in runs)
    topd = per_day.most_common(1)[0][0]
    Wd = X0 + len(weeks) * (CW + G) + 40
    H = Y0 + 7 * (CW + G) + 36
    b = [T(0, 12, "per hari dalam seminggu", "pt")]
    Y0 += 16
    H += 16
    for wi, w in enumerate(weeks):
        x = X0 + wi * (CW + G)
        firsts = [w + timedelta(days=k) for k in range(7) if (w + timedelta(days=k)).day == 1]
        if wi == 0 or firsts:
            first = max(w, SINCE.date()) if wi == 0 else firsts[0]
            b.append(T(x, Y0 - 10, BULAN[first.month]))
        for d in range(7):
            day = w + timedelta(days=d)
            y = Y0 + d * (CW + G)
            v = km.get(day, 0)
            if day > today or day < SINCE.date():
                cls = "cf"
            elif v == 0:
                cls = "c0"
            else:
                cls = "c1" if v < 4 else "c2" if v < 8 else "c3"
            b.append(f'<rect class="{cls}" x="{x}" y="{y}" width="{CW}" height="{CW}" rx="4"/>')
            if v:
                b.append(f'<rect class="hit" x="{x - G / 2}" y="{y - G / 2}" width="{CW + G}" height="{CW + G}"'
                         + tip(f"{num(v)} km", f"{HARI_PANJANG[d]}, {tgl(day)}") + "/>")
    xe = X0 + len(weeks) * (CW + G) + 6
    for d in range(7):
        y = Y0 + d * (CW + G) + CW / 2 + 4
        b.append(T(X0 - 8, y, HARI[d], "ax strong" if d == topd else "ax", "end"))
        b.append(T(xe, y, f"{per_day[d]}×", "val" if d == topd else "ax"))
    b.append(legend(X0, H - 8, [("rect", "c1", "< 4 km"), ("rect", "c2", "4–8 km"), ("rect", "c3", "≥ 8 km")]))
    return svg(Wd, H, "".join(b), "Kalender lari per hari", f' width="{Wd:.0f}" height="{H:.0f}"'), Wd


def chart_hours(runs):
    """Bars per start hour: 04-19 always, widened to every hour that has a run."""
    hours = Counter(r["start"].hour for r in runs)
    h0, h1 = min(4, min(hours)), max(20, max(hours) + 1)
    X0, X1, Y0, Y1 = 30, 420, 44, 204
    top = max(hours.values())
    bw = (X1 - X0) / (h1 - h0)
    b = [T(0, 12, "jam mulai lari", "pt")]
    for k in range(0, top + 1, 5):
        y = Y1 - k / top * (Y1 - Y0)
        b.append(hline(X0, X1, y))
        b.append(T(X0 - 8, y + 4, str(k), anchor="end"))
    for h in range(h0, h1):
        x = X0 + (h - h0) * bw + 3
        v = hours.get(h, 0)
        if v:
            hh = v / top * (Y1 - Y0)
            b.append(col(x, Y1 - hh, bw - 6, hh, "bar hi" if h < 8 else "bar"))
            b.append(T(x + (bw - 6) / 2, Y1 - hh - 6, str(v), "val", "middle"))
            b.append(f'<rect class="hit" x="{x - 3:.1f}" y="{Y0}" width="{bw:.1f}" height="{Y1 - Y0}"'
                     + tip(f"{v} lari", f"mulai jam {h:02d}.00–{h:02d}.59") + "/>")
        if h % 2 == 0:
            b.append(T(x + (bw - 6) / 2, Y1 + 16, f"{h:02d}", anchor="middle"))
    return svg(440, Y1 + 24, "".join(b), "Jam mulai lari", f' width="440" height="{Y1 + 24}"')


# ---------------- 7. longest run ----------------

def chart_longest(r, sp):
    """Up to three panels on one distance axis: time per km (needs a full km),
    heart rate, elevation (needs elevation in the GPX). A panel without data
    is left out and the ones below move up."""
    X0, X1, GAP = 64, 900, 54
    dmax = r["dist"] / 1000

    def X(km):
        return X0 + km / dmax * (X1 - X0)

    sl = [s for s in r["slices"] if s["hr"]]
    sm = [(sl[i]["d"] / 1000, statistics.mean(x["hr"] for x in sl[max(0, i - 2):i + 3])) for i in range(len(sl))]
    ep, last = [], None
    for p in r["pts"]:
        if p["ele"] is not None and (last is None or p["d"] - last >= 50):
            ep.append((p["d"] / 1000, p["ele"]))
            last = p["d"]
    y, spans = 40, {}
    for name, h, has in (("A", 120, sp), ("B", 102, sm), ("C", 80, ep)):
        if has:
            spans[name] = (y, y + h)
            y += h + GAP
    if not spans:
        return ""
    top, bottom = min(a for a, _ in spans.values()), max(b for _, b in spans.values())

    b = []
    if "A" in spans:  # time per km
        A0, A1 = spans["A"]
        ss = [s["s"] for s in sp]
        lo, hi = math.floor((min(ss) - 20) / 60) * 60, math.ceil((max(ss) + 20) / 60) * 60

        def YA(s):
            return A0 + (s - lo) / (hi - lo) * (A1 - A0)

        b.append(T(X0, A0 - 18, "waktu per km (termasuk berhenti) · makin atas makin cepat", "pt"))
        for s in range(lo, hi + 1, 60):
            b.append(hline(X0, X1, YA(s)))
            b.append(T(X0 - 10, YA(s) + 4, mmss(s), anchor="end"))
        b.append('<polyline class="line" points="' + " ".join(f"{X(s['km'] - 0.5):.1f},{YA(s['s']):.1f}" for s in sp) + '"/>')
        slow, fast = max(sp, key=lambda s: s["s"]), min(sp, key=lambda s: s["s"])
        for s in sp:
            b.append(dot(X(s["km"] - 0.5), YA(s["s"]), "hl"))
        b.append(T(X(slow["km"] - 0.5), YA(slow["s"]) + 18, f"km {slow['km']}: {mmss(slow['s'])} · +{slow['gain']:.0f} m", "val", "middle"))
        b.append(T(X(fast["km"] - 0.5), YA(fast["s"]) - 10, f"km {fast['km']}: {mmss(fast['s'])}", "val", "middle"))
    if "B" in spans:  # heart rate
        B0, B1 = spans["B"]
        hlo, hhi = math.floor((min(h for _, h in sm) - 4) / 10) * 10, math.ceil((max(h for _, h in sm) + 4) / 10) * 10

        def YB(h):
            return B1 - (h - hlo) / (hhi - hlo) * (B1 - B0)

        b.append(T(X0, B0 - 18, "detak jantung (bpm)", "pt"))
        for h in range(hlo, hhi + 1, 10):
            b.append(hline(X0, X1, YB(h)))
            b.append(T(X0 - 10, YB(h) + 4, str(h), anchor="end"))
        b.append('<polyline class="line" points="' + " ".join(f"{X(d):.1f},{YB(h):.1f}" for d, h in sm) + '"/>')
    if "C" in spans:  # elevation
        C0, C1 = spans["C"]
        elo, ehi = min(e for _, e in ep), max(e for _, e in ep)

        def YC(e):
            return C1 - (e - elo) / max(1, ehi - elo) * (C1 - C0)

        gain = sum(s["gain"] for s in sp)
        b.append(T(X0, C0 - 18, f"elevasi · naik total {gain:.0f} m", "pt"))
        b.append(hline(X0, X1, C1, "axis"))
        pl = " ".join(f"{X(d):.1f},{YC(e):.1f}" for d, e in ep)
        b.append(f'<polygon class="area" points="{X(ep[0][0]):.1f},{C1} {pl} {X(ep[-1][0]):.1f},{C1}"/>')
        b.append(f'<polyline class="line" points="{pl}"/>')
        b.append(T(X0 - 10, YC(ehi) + 4, f"{ehi:.0f} m", anchor="end"))
        b.append(T(X0 - 10, YC(elo) + 4, f"{elo:.0f} m", anchor="end"))
    for k in range(0, int(dmax) + 1):
        b.append(T(X(k), bottom + 18, f"{k}" + (" km" if k == int(dmax) else ""), anchor="middle"))
    b.append(f'<line class="xhair" x1="0" x2="0" y1="{top - 6}" y2="{bottom}"/>')
    # crosshair rows every 100 m
    rows, k = [], 0
    by_km = {s["km"]: s for s in sp}
    for i, p in enumerate(r["pts"]):
        if p["d"] >= k * 100:
            s = by_km.get(int(p["d"] // 1000) + 1)
            lines = [f"km {num(p['d'] / 1000)}"]
            if sm:
                hr = next((h for d, h in sm if d * 1000 >= p["d"]), sm[-1][1])
                lines.append(f"HR {hr:.0f} bpm")
            if p["ele"] is not None:
                lines.append(f"elevasi {p['ele']:.0f} m")
            if s:
                lines.append(f"km {s['km']}: {mmss(s['s'])}")
            rows.append([round(p["d"] / 1000, 2)] + lines)
            k += 1
    cfg = json.dumps({"x0": X0, "x1": X1, "dmax": round(dmax, 3), "rows": rows}, ensure_ascii=False)
    return svg(W, bottom + 26, "".join(b), "Waktu per km, detak jantung, dan elevasi lari terjauh",
               f' data-xhair="{esc(cfg)}"')


# ---------------- 8. load ----------------

def chart_load(ratio):
    """A meter in plain HTML so it reflows on a phone instead of scrolling."""
    top = LOAD_ZONES[-1][1]
    pos = min(max(ratio / top * 100, 3), 97)
    track = "".join(f'<span class="{cls}" style="flex:{hi - lo}"></span>' for lo, hi, _, cls, _ in LOAD_ZONES)
    keys = []
    for lo, hi, name, cls, icon in LOAD_ZONES:
        rng = f"< {num(hi)}×" if lo == 0 else f"> {num(lo)}×" if hi == top else f"{num(lo)}–{num(hi)}×"
        cur = lo <= min(ratio, top - 0.01) < hi
        keys.append(f'<span class="{"cur" if cur else ""}"><i class="{cls}"></i>{icon} {name} {rng}</span>')
    return (f'<div class="meter" role="img" aria-label="Rasio beban {num(ratio)}×">'
            f'<div class="pin" style="left:{pos:.1f}%">{num(ratio)}×</div><div class="track">{track}</div>'
            f'<div class="keys">{"".join(keys)}</div></div>')


# ---------------- page ----------------

CSS = """
*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
:root {
  color-scheme: light;
  --paper:#f5f5f5; --card:#ffffff; --ink:#2d3142; --muted:#4f5d75; --soft:#6b7488;
  --hair:rgba(45,49,66,0.12); --grid:#e6e7eb; --axis:#c3c6cf; --wash:rgba(45,49,66,0.06);
  --accent:#bf4520; --blue:#2a78d6; --stop:#c9ccd4; --bar:#8a93a6; --ctxline:rgba(45,49,66,0.16);
  --z1:#e9a184; --z2:#dd7a52; --z3:#bf4520; --z4:#8f3014; --z5:#5e1f0b;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --paper:#121316; --card:#1c1d21; --ink:#e8e9ee; --muted:#a8aec0; --soft:#8b92a5;
    --hair:rgba(255,255,255,0.10); --grid:#2c2e34; --axis:#3b3e46; --wash:rgba(255,255,255,0.06);
    --accent:#e0663a; --blue:#3987e5; --stop:#4a4e59; --bar:#6b7286; --ctxline:rgba(255,255,255,0.14);
    --z1:#8a3a1c; --z2:#b44d26; --z3:#d9663a; --z4:#ef8f63; --z5:#f7c0a0;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --paper:#121316; --card:#1c1d21; --ink:#e8e9ee; --muted:#a8aec0; --soft:#8b92a5;
  --hair:rgba(255,255,255,0.10); --grid:#2c2e34; --axis:#3b3e46; --wash:rgba(255,255,255,0.06);
  --accent:#e0663a; --blue:#3987e5; --stop:#4a4e59; --bar:#6b7286; --ctxline:rgba(255,255,255,0.14);
  --z1:#8a3a1c; --z2:#b44d26; --z3:#d9663a; --z4:#ef8f63; --z5:#f7c0a0;
}
body { font-family:'Geist',system-ui,sans-serif; background:var(--paper); color:var(--ink); padding:2.5rem 1.5rem 4rem; }
.wrap { max-width:1040px; margin:0 auto; }
.eyebrow { font-family:'Geist Mono',ui-monospace,monospace; font-size:0.66rem; letter-spacing:0.18em; text-transform:uppercase; color:var(--muted); margin-bottom:0.5rem; }
h1 { font-family:'Instrument Serif',serif; font-weight:400; font-size:clamp(1.8rem,4vw,2.6rem); letter-spacing:-0.02em; line-height:1.1; margin-bottom:0.5rem; }
.sub { color:var(--muted); font-size:0.85rem; margin-bottom:2rem; }
.hero { display:grid; grid-template-columns:1.9fr 1fr 1fr 1fr 1fr; gap:16px; margin-bottom:20px; }
.tile { background:var(--card); border:1px solid var(--hair); border-radius:6px; padding:1.1rem 1.25rem; }
.tile .k { font-size:0.75rem; color:var(--muted); margin-bottom:0.35rem; }
.tile .v { font-size:1.6rem; font-weight:600; line-height:1.1; white-space:nowrap; }
.tile.big .v { font-size:3.4rem; letter-spacing:-0.02em; }
.tile .d { font-size:0.75rem; color:var(--muted); margin-top:0.35rem; line-height:1.4; }
.sec { background:var(--card); border:1px solid var(--hair); border-radius:6px; padding:1.4rem 1.5rem 1.1rem; margin-bottom:20px; }
.sec h2 { font-family:'Instrument Serif',serif; font-weight:400; font-size:clamp(1.3rem,2.6vw,1.65rem); line-height:1.2; margin-bottom:0.45rem; }
.lead { color:var(--muted); font-size:0.88rem; line-height:1.55; max-width:76ch; margin-bottom:1.1rem; }
.lead b { color:var(--ink); font-weight:600; }
.note { color:var(--soft); font-size:0.75rem; line-height:1.5; margin-top:0.7rem; max-width:90ch; }
.chart { overflow-x:auto; }
.chart svg { display:block; width:100%; min-width:760px; height:auto; }
.chart.fit svg { min-width:0; width:auto; max-width:100%; }
.two { display:flex; flex-wrap:wrap; gap:28px 48px; align-items:flex-end; }
.chip { display:inline-block; font-size:0.78rem; font-weight:600; padding:1px 10px; border-radius:99px; border:1px solid var(--hair); color:var(--ink); margin-right:4px; }
details { margin-top:0.8rem; font-size:0.8rem; color:var(--muted); }
summary { cursor:pointer; font-family:'Geist Mono',monospace; font-size:0.68rem; letter-spacing:0.1em; text-transform:uppercase; }
table { border-collapse:collapse; margin-top:0.6rem; font-size:0.8rem; color:var(--ink); }
th, td { padding:4px 12px 4px 0; border-bottom:1px solid var(--hair); text-align:left; white-space:nowrap; }
th { font-weight:600; color:var(--muted); }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
footer { font-family:'Geist Mono',monospace; font-size:0.62rem; color:var(--muted); letter-spacing:0.1em; border-top:1px solid var(--hair); padding-top:1rem; line-height:1.7; }
#tip { position:fixed; z-index:10; pointer-events:none; background:var(--card); border:1px solid var(--hair); border-radius:6px; padding:8px 10px; box-shadow:0 6px 20px rgba(0,0,0,0.14); font-size:12px; line-height:1.45; color:var(--muted); max-width:300px; }
#tip .v { font-weight:600; font-size:13px; color:var(--ink); }
@media (max-width:860px) { .hero { grid-template-columns:1fr 1fr; } .tile.big { grid-column:1 / -1; } }
@media (max-width:600px) { body { padding:1.5rem 16px 3rem; } .sec { padding:1.1rem 1rem 0.9rem; } }
/* svg */
.grid { stroke:var(--grid); stroke-width:1; }
.axis { stroke:var(--axis); stroke-width:1; }
.ax { font:500 10.5px 'Geist Mono',ui-monospace,monospace; fill:var(--soft); }
.ax.strong { font-weight:700; fill:var(--ink); }
.lg { font:500 11.5px 'Geist',system-ui,sans-serif; fill:var(--muted); }
.pt { font:600 11.5px 'Geist',system-ui,sans-serif; fill:var(--ink); }
.val { font:600 11px 'Geist',system-ui,sans-serif; fill:var(--ink); }
.ann { font:500 11.5px 'Geist',system-ui,sans-serif; fill:var(--ink); }
.dot { stroke:var(--card); stroke-width:2; }
.s-kept, .hl, .start { fill:var(--accent); }
.s-drop { fill:var(--blue); }
.ctx { fill:var(--bar); }
.link { stroke:var(--soft); stroke-width:1.5; opacity:0.5; }
.run { fill:var(--accent); }
.stop { fill:var(--stop); }
.med { stroke:var(--ink); stroke-width:2; stroke-linecap:round; }
.bar { fill:var(--bar); }
.bar.hi { fill:var(--accent); }
.z1 { fill:var(--z1); } .z2 { fill:var(--z2); } .z3 { fill:var(--z3); } .z4 { fill:var(--z4); } .z5 { fill:var(--z5); }
.c0 { fill:none; stroke:var(--grid); } .cf { fill:none; stroke:var(--grid); opacity:0.35; }
.c1 { fill:var(--z1); } .c2 { fill:var(--z3); } .c3 { fill:var(--z5); }
.panel { fill:var(--wash); }
.trk-ctx { fill:none; stroke:var(--ctxline); stroke-width:1.2; stroke-linejoin:round; stroke-linecap:round; }
.trk { fill:none; stroke:var(--accent); stroke-width:1.8; stroke-linejoin:round; stroke-linecap:round; opacity:0.85; }
.lbl-map { font:600 10.5px 'Geist',system-ui,sans-serif; fill:var(--ink); paint-order:stroke; stroke:var(--card); stroke-width:3px; stroke-linejoin:round; }
.line { fill:none; stroke:var(--accent); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
.area { fill:var(--accent); opacity:0.10; }
.xhair { stroke:var(--muted); stroke-width:1; opacity:0; pointer-events:none; }
.meter { position:relative; padding-top:30px; max-width:880px; }
.meter .track { display:flex; gap:2px; height:10px; }
.meter .track span, .meter .keys i { border-radius:3px; }
.meter .pin { position:absolute; top:0; transform:translateX(-50%); display:flex; flex-direction:column; align-items:center; font-size:0.8rem; font-weight:600; line-height:16px; color:var(--ink); }
.meter .pin::after { content:""; width:12px; height:12px; margin-top:13px; border-radius:50%; background:var(--ink); border:2px solid var(--card); }
.meter .keys { display:flex; flex-wrap:wrap; gap:6px 18px; margin-top:14px; font-size:0.78rem; color:var(--muted); }
.meter .keys i { display:inline-block; width:10px; height:10px; margin-right:6px; vertical-align:-1px; }
.meter .keys .cur { color:var(--ink); font-weight:600; }
.m-low { background:var(--bar); opacity:0.45; } .m-ok { background:var(--good); } .m-warn { background:var(--warn); } .m-crit { background:var(--crit); }
.hit { fill:transparent; outline:none; }
.hit.hot, .hit:focus-visible { fill:var(--wash); }
"""

JS = """
const tip = document.getElementById('tip');
function show(lines, x, y) {
  tip.replaceChildren();
  lines.forEach((line, i) => {
    const d = document.createElement('div');
    d.textContent = line;
    if (i === 0) d.className = 'v';
    tip.appendChild(d);
  });
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  tip.style.left = Math.max(8, left) + 'px';
  tip.style.top = Math.max(8, top) + 'px';
}
function hide() { tip.hidden = true; document.querySelectorAll('.hot').forEach(h => h.classList.remove('hot')); }
document.addEventListener('pointermove', e => {
  if (e.target.closest('svg[data-xhair]')) return;
  const el = e.target.closest('[data-tip]');
  document.querySelectorAll('.hot').forEach(h => { if (h !== el) h.classList.remove('hot'); });
  if (!el) { tip.hidden = true; return; }
  el.classList.add('hot');
  show(el.dataset.tip.split('\\n'), e.clientX, e.clientY);
});
document.addEventListener('focusin', e => {
  const el = e.target.closest('[data-tip]');
  if (!el) return;
  const r = el.getBoundingClientRect();
  show(el.dataset.tip.split('\\n'), r.left + r.width / 2, r.bottom);
});
document.addEventListener('focusout', hide);
document.querySelectorAll('svg[data-xhair]').forEach(svg => {
  const cfg = JSON.parse(svg.dataset.xhair);
  const line = svg.querySelector('.xhair');
  svg.addEventListener('pointermove', e => {
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    if (p.x < cfg.x0 || p.x > cfg.x1) { line.style.opacity = 0; tip.hidden = true; return; }
    const km = (p.x - cfg.x0) / (cfg.x1 - cfg.x0) * cfg.dmax;
    let best = cfg.rows[0];
    for (const r of cfg.rows) if (Math.abs(r[0] - km) < Math.abs(best[0] - km)) best = r;
    const x = cfg.x0 + best[0] / cfg.dmax * (cfg.x1 - cfg.x0);
    line.setAttribute('x1', x); line.setAttribute('x2', x); line.style.opacity = 1;
    show(best.slice(1), e.clientX, e.clientY);
  });
  svg.addEventListener('pointerleave', () => { line.style.opacity = 0; tip.hidden = true; });
});
"""


def section(sid, eyebrow, title, lead, chart, tbl="", note="", fit=False):
    return (f'<section class="sec" id="{sid}"><p class="eyebrow">{esc(eyebrow)}</p><h2>{esc(title)}</h2>'
            f'<p class="lead">{lead}</p><div class="chart{" fit" if fit else ""}">{chart}</div>'
            f'{tbl}{f"<p class=note>{note}</p>" if note else ""}</section>')


def build():
    fetched, raw_acts = read_activities()
    try:
        f = datetime.fromisoformat(fetched)
        fetched = f"{f.day} {BULAN[f.month]} {f.year}, {f:%H:%M} UTC"
    except ValueError:
        pass
    has_hr = {}

    def hr_of(a):
        if a["id"] not in has_hr:
            has_hr[a["id"]] = gpx_has_hr(GPX_DIR / f"{a['id']}.gpx")
        return has_hr[a["id"]]

    in_window = [a for a in raw_acts if a["start"] >= SINCE]
    kept, dup_of = dedupe(in_window, has_hr=hr_of)
    merged = len(dup_of)
    raw_runs = [a for a in in_window if a["type"] == "Run"]
    # Totals, weekly load and the calendar count every kept outdoor run;
    # only the point-by-point sections need a usable GPX track (runs).
    outdoor = [a for a in kept if a["type"] == "Run" and not a["trainer"]]
    runs = sorted((r for r in (analyse(a) for a in outdoor if (GPX_DIR / f"{a['id']}.gpx").exists()) if r),
                  key=lambda r: r["start"])
    if len(runs) < 3:
        raise SystemExit("not enough runs since SINCE yet")
    tracked = {r["id"]: r for r in runs}
    summ = sorted((tracked.get(a["id"], {**a, "place": OTHER}) for a in outdoor), key=lambda r: r["start"])
    hr_runs = [r for r in runs if r["has_hr"]]
    now = datetime.now()
    today = now.date()
    months = sorted({(r["start"].year, r["start"].month) for r in runs})
    weeks = []
    w = monday(SINCE)
    while w <= monday(today):
        weeks.append(w)
        w += timedelta(days=7)
    wk_km = {w: 0.0 for w in weeks}
    for r in summ:
        wk_km[monday(r["start"])] += r["km"]

    # 1. honest totals
    by_id = {a["id"]: a for a in in_window}
    pairs = sorted(((by_id[k], by_id[d]) for d, k in dup_of.items() if by_id[d]["type"] == "Run"),
                   key=lambda p: p[0]["start"])
    watchy = all(hr_of(k) and not hr_of(d) for k, d in pairs)
    labels = ("jam · dipakai", "HP · dibuang") if watchy else ("dipakai", "dibuang")
    km_all = sum(r["km"] for r in summ)
    km_raw = sum(a["km"] for a in raw_runs if not a["trainer"])
    hours = sum(r["moving_s"] for r in summ) / 3600
    longest = max(summ, key=lambda r: r["km"])
    no_gpx = len(summ) - len(runs)
    span = f"{BULAN_PANJANG[SINCE.month]} – {BULAN_PANJANG[today.month]} {today.year}"
    hero = f"""
    <div class="hero">
      <div class="tile big"><div class="k">Total lari sejak {tgl(SINCE)}</div><div class="v">{num(km_all)} km</div>
        <div class="d">bukan {num(km_raw)} km · {len(pairs)} lari tercatat dua kali, sekarang digabung</div></div>
      <div class="tile"><div class="k">Lari</div><div class="v">{len(summ)}</div><div class="d">{len(hr_runs)} dengan detak jantung{f" · {no_gpx} tanpa GPX" if no_gpx else ""}</div></div>
      <div class="tile"><div class="k">Waktu bergerak</div><div class="v">{num(hours)} jam</div><div class="d">rata-rata {mmss(hours * 3600 / km_all) if km_all else "-"} /km</div></div>
      <div class="tile"><div class="k">Terjauh</div><div class="v">{num(longest['km'])} km</div><div class="d">{tgl(longest['start'])}{f" · {esc(longest['place'])}" if longest["id"] in tracked else ""}</div></div>
      <div class="tile"><div class="k">Minggu ini</div><div class="v">{num(wk_km[weeks[-1]])} km</div><div class="d">minggu {tgl(weeks[-1])}</div></div>
    </div>"""
    out = []
    if pairs:
        diffs = [(d["km"] / k["km"] - 1) * 100 for k, d in pairs]
        longer = sum(1 for x in diffs if x > 0)
        med = statistics.median(diffs)
        who = ("HP" if watchy else "Duplikat")
        if abs(med) < 3:
            title = f"{'HP dan jam' if watchy else 'Kedua versi'} hampir sepakat: selisih median {med:+.0f}%"
        else:
            title = (f"{who} selalu mencatat lebih jauh" if longer == len(pairs)
                     else f"{who} lebih jauh di {longer} dari {len(pairs)} lari")
        lead = (f"Dari {tgl(pairs[0][0]['start'])} sampai {tgl(pairs[-1][0]['start'])}, <b>{len(pairs)} lari</b> masuk dua kali"
                + (" (aplikasi di HP dan jam)" if watchy else "") + f". Selisih jaraknya median <b>{med:+.0f}%</b>, "
                f"paling jauh <b>{max(diffs, key=abs):+.0f}%</b>. Halaman ini memakai versi "
                + ("jam: jarak yang dicatat jam sendiri, plus detak jantungnya." if watchy
                   else "dengan detak jantung, atau unggahan pertama."))
        dev = [r for r in runs if "strava_km" in r]
        recount = ""
        if dev:
            short = sum(r["km"] for r in dev) - sum(r["strava_km"] for r in dev)
            recount = (f"Jarak jam = yang dicatat jam sendiri di file FIT. Hitungan ulang Strava dari titik GPS "
                       f"{len(dev)} lari jam totalnya {num(short)} km lebih pendek, karena GPS jam sempat macet; "
                       "angka Strava itu tidak dipakai di halaman ini.")
        out.append(section("jujur", "Angka yang jujur · dua pencatat", title, lead, chart_twins(pairs, labels),
                           table(["Tanggal", f"#{labels[0]} (km)", f"#{labels[1]} (km)", "#Selisih"],
                                 [(tgl(k["start"]), num(k["km"], 2), num(d["km"], 2), f"{x:+.0f}%")
                                  for (k, d), x in zip(pairs, diffs)]),
                           recount or "Mana yang benar belum bisa dipastikan tanpa rute yang jaraknya sudah diketahui."))

    # 2. run / walk
    milestone = next((r for r in runs if r["walk_pct"] < 2 and r["km"] >= 4), None)
    walk_m = [(m, statistics.median(r["walk_pct"] for r in runs if (r["start"].year, r["start"].month) == (y_, m)))
              for y_, m in months]
    title = (f"{tgl(milestone['start'])}: {num(milestone['km'])} km pertama tanpa berhenti" if milestone
             else f"Porsi jalan/berhenti {walk_m[0][1]:.0f}% → {walk_m[-1][1]:.0f}%")
    lead = ("Median porsi jalan atau berhenti per lari: "
            + " → ".join(f"{BULAN[m]} <b>{v:.0f}%</b>" for m, v in walk_m) + ". "
            + "Tiap baris satu lari, panjangnya sesuai durasi.")
    out.append(section("lari-jalan", "Lari vs jalan", title, lead, chart_strip(runs, milestone),
                       table(["Tanggal", "Tempat", "#Jarak", "#Durasi", "#Jalan/berhenti"],
                             [(tgl(r["start"]), r["place"], f"{num(r['km'])} km", f"{r['total_s'] / 60:.0f} mnt",
                               f"{r['walk_pct']:.0f}%") for r in runs]),
                       "Jalan/berhenti = potongan 10 detik yang lebih lambat dari 12:00 /km, termasuk lampu merah, "
                       "istirahat, dan pemanasan jalan kaki. "
                       + (f"{sum(r['retimed'] for r in runs)} lari yang GPS jamnya macet dihitung dari kecepatan "
                          "yang dicatat jam: totalnya pas, tapi per km-nya perkiraan." if any(r["retimed"] for r in runs) else "")))

    # 3. same heart: needs two months with at least two runs in the band each
    paced = [r for r in hr_runs if r["pace_hr"]]
    by_m = defaultdict(list)
    for r in paced:
        by_m[(r["start"].year, r["start"].month)].append(r["pace_hr"])
    med_m = [(m, statistics.median(v), len(v)) for (y_, m), v in sorted(by_m.items()) if len(v) >= 2]
    if len(paced) >= 4 and len(med_m) >= 2:
        recent = [r for r in paced if r["start"].date() >= today - timedelta(days=28)]
        hl_place = (Counter(r["place"] for r in recent if r["place"] != OTHER).most_common(1) or [(paced[-1]["place"], 0)])[0][0]
        solid = [x for x in med_m if x[2] >= 3]
        first, last = (solid[0], solid[-1]) if len(solid) >= 2 else (med_m[0], med_m[-1])
        gain = first[1] - last[1]
        if abs(gain) < 10:
            title = (f"Di detak yang sama, pace stabil dari {BULAN_PANJANG[first[0]]} ke {BULAN_PANJANG[last[0]]} "
                     f"({mmss(first[1])} → {mmss(last[1])} /km)")
        else:
            title = (f"Di detak yang sama, {secs(abs(gain))} per km lebih " + ("cepat" if gain > 0 else "lambat")
                     + f" dari {BULAN_PANJANG[first[0]]} ke {BULAN_PANJANG[last[0]]}")
        lead = (f"Median pace saat HR {HR_LO}–{HR_HI} bpm (bagian yang lagi lari saja): "
                + " → ".join(f"{BULAN[m]} <b>{mmss(v)}</b> ({n} lari)" for m, v, n in med_m) + ".")
        hr_months = sorted({(r["start"].year, r["start"].month) for r in hr_runs})
        thin = [m for y_, m in hr_months if m not in {x[0] for x in med_m}]
        if thin:
            lead += (" " + ", ".join(BULAN_PANJANG[m] for m in thin)
                     + f" tidak cukup data: larinya jarang berada di {HR_LO}–{HR_HI} bpm (lebih sering di atasnya).")
        hp = [r for r in paced if r["place"] == hl_place]
        prev4 = [wk_km[w] for w in weeks[-5:-1]]
        ratio = wk_km[weeks[-1]] / statistics.mean(prev4) if prev4 and statistics.mean(prev4) else 0
        if len(hp) >= 4:
            a_, z_ = statistics.mean(r["pace_hr"] for r in hp[:2]), statistics.mean(r["pace_hr"] for r in hp[-2:])
            if z_ - a_ >= 15:
                lead += (f" Tapi di {hl_place}, rute yang sama, pace di detak ini melambat dari <b>{mmss(a_)}</b> "
                         f"({tgl(hp[0]['start'])}–{tgl(hp[1]['start'])}) ke <b>{mmss(z_)}</b> "
                         f"({tgl(hp[-2]['start'])}–{tgl(hp[-1]['start'])})"
                         + (f", bareng volume minggu ini {num(ratio)}× biasanya. Bisa jadi tanda capek, layak dipantau."
                            if ratio >= 1.3 else ". Layak dipantau."))
            elif a_ - z_ >= 15:
                lead += f" Di {hl_place}, rute yang sama, pace di detak ini membaik dari <b>{mmss(a_)}</b> ke <b>{mmss(z_)}</b>."
        out.append(section("jantung", "Jantung yang sama", title, lead, chart_same_heart(runs, weeks, wk_km, hl_place),
                           table(["Tanggal", "Tempat", "#Pace @HR", "#HR rata-rata"],
                                 [(tgl(r["start"]), r["place"], mmss(r["pace_hr"]), f"{r['avg_hr']:.0f}") for r in paced],
                                 "Tabel pace")
                           + table(["Minggu", "#Km"], [(tgl(w), num(wk_km[w])) for w in weeks], "Tabel km per minggu"),
                           f"Pace dihitung hanya dari potongan 10 detik dengan HR {HR_LO}–{HR_HI} bpm dan kecepatan lari, "
                           f"setelah {WARMUP_S // 60} menit pertama (HR masih telat naik), minimal {MIN_BAND_S // 60} menit "
                           "per lari. Rute beda tanjakan dan cuaca tetap ikut berpengaruh."))

    # 4. intensity mix
    if hr_runs:
        hard_m = []
        for y_, m in months:
            rs = [r for r in hr_runs if (r["start"].year, r["start"].month) == (y_, m)]
            if rs:
                tot = sum(sum(r["zones"]) for r in rs)
                hard = sum(z for r in rs for (lo, _, _), z in zip(BANDS, r["zones"]) if lo >= HARD_HR)
                hard_m.append((m, hard / tot * 100 if tot else 0))
        peak = max(hard_m, key=lambda x: x[1])
        title = (f"{BULAN_PANJANG[peak[0]]} paling ngebut: {peak[1]:.0f}% waktu di ≥ {HARD_HR} bpm"
                 + (f", {BULAN_PANJANG[hard_m[-1][0]]} turun ke {hard_m[-1][1]:.0f}%" if peak != hard_m[-1] else ""))
        lr = max(hr_runs, key=lambda r: r["km"])
        easy = sum(z for (lo, _, _), z in zip(BANDS, lr["zones"]) if lo < 140) / (sum(lr["zones"]) or 1) * 100
        lead = (f"Porsi waktu di ≥ {HARD_HR} bpm per bulan: "
                + " → ".join(f"{BULAN[m]} <b>{v:.0f}%</b>" for m, v in hard_m)
                + f". Lari terjauh ({num(lr['km'])} km, {tgl(lr['start'])}) <b>{easy:.0f}%</b> di bawah 140 bpm.")
        hrmax = max(p["hr"] for r in hr_runs for p in r["pts"] if p["hr"])
        out.append(section("intensitas", "Intensitas", title, lead, chart_mix(hr_runs),
                           table(["Tanggal", "#HR rata-rata"] + [f"#{lbl}" for _, _, lbl in BANDS],
                                 [(tgl(r["start"]), f"{r['avg_hr']:.0f}")
                                  + tuple(f"{z / (sum(r['zones']) or 1) * 100:.0f}%" for z in r["zones"]) for r in hr_runs]),
                           f"Rentang dipatok kasar dari HR tertinggi yang pernah tercatat ({hrmax} bpm), bukan HR maksimal asli. "
                           "Kalau umur atau HR maks diketahui, zonanya bisa dikalibrasi."))

    # 5. where
    tops = []
    for y_, m in months:
        rs = [r for r in runs if (r["start"].year, r["start"].month) == (y_, m) and r["place"] != OTHER]
        if rs:
            c = Counter(r["place"] for r in rs)
            kmp = defaultdict(float)
            for r in rs:
                kmp[r["place"]] += r["km"]
            tops.append((m, max(c, key=lambda p: (c[p], kmp[p]))))
    groups = []
    for m, p in tops:
        if groups and groups[-1][1] == p:
            groups[-1][0].append(m)
        else:
            groups.append(([m], p))
    if not groups:
        title = "Rute per bulan"
    elif len(groups) == 1:
        title = f"Markasnya {groups[0][1]}"
    elif len(groups) == 2:
        title = f"{month_range(groups[0][0])} di {groups[0][1]}, {month_range(groups[1][0])} pindah ke {groups[1][1]}"
    else:
        title = " → ".join(f"{month_range(ms)} {p}" for ms, p in groups)
    place_n = Counter(r["place"] for r in runs)
    lead = ("Titik mulai per tempat: " + ", ".join(f"{p} <b>{n}</b>" for p, n in place_n.most_common()) + ". "
            "Garis oranye = rute bulan itu, abu-abu = semua rute lain sebagai konteks.")
    out.append(section("di-mana", "Di mana", title, lead, chart_map(runs, months),
                       table(["Bulan"] + [f"#{p}" for p, *_ in PLACES] + [f"#{OTHER}"],
                             [(BULAN_PANJANG[m],) + tuple(str(sum(1 for r in runs if (r["start"].year, r["start"].month) == (y_, m)
                                                                and r["place"] == p)) for p in [p for p, *_ in PLACES] + [OTHER])
                              for y_, m in months]),
                       "Tempat dicocokkan dari titik GPS pertama tiap lari (radius 1,5 km). Peta ini memperlihatkan "
                       "titik mulai di dekat rumah, jadi jangan dibagikan tanpa memotong awal dan akhir rute."))

    # 6. when: every kept outdoor run, GPX or not
    per_day = Counter(r["start"].weekday() for r in summ)
    topd, n_top = per_day.most_common(1)[0]
    morning = sum(1 for r in summ if r["start"].hour < 8)
    day_runs = [r for r in summ if r["start"].weekday() == topd]
    dp, dn = Counter(r["place"] for r in day_runs).most_common(1)[0]
    title = f"{n_top} dari {len(summ)} lari jatuh di hari {HARI_PANJANG[topd]}"
    lead = (f"Itu <b>{n_top / len(summ) * 100:.0f}%</b> dari semua lari"
            + (f"; {dn} di antaranya mulai dari {dp}. " if dp != OTHER else ". ")
            + f"<b>{morning} dari {len(summ)}</b> lari mulai sebelum jam 8 pagi.")
    cal, wd = chart_week(summ, weeks, today)
    out.append(section("kapan", "Kapan", title, lead,
                       f'<div class="two">{cal}{chart_hours(summ)}</div>',
                       table(["Hari", "#Lari"], [(HARI_PANJANG[d], str(per_day[d])) for d in range(7)], "Tabel per hari")
                       + table(["Jam mulai", "#Lari"], sorted((f"{h:02d}.00", str(n)) for h, n in
                                                              Counter(r["start"].hour for r in summ).items()),
                               "Tabel jam mulai"),
                       fit=True))

    # 7. longest run: each comparison only where both parts have the data
    if hr_runs:
        lr = max(hr_runs, key=lambda r: r["km"])
        sp = splits(lr["pts"])
        cut = 0.6 * lr["dist"]
        a_pts = [p for p in lr["pts"] if p["d"] <= cut]
        z_pts = [p for p in lr["pts"] if p["d"] > cut]

        def pace_of(ps):
            dt, dd = (ps[-1]["s"] - ps[0]["s"], ps[-1]["d"] - ps[0]["d"]) if ps else (0, 0)
            return dt / (dd / 1000) if dt > 0 and dd > 0 else None

        def hr_of_part(ps):
            hrs = [p["hr"] for p in ps if p["hr"]]
            return statistics.mean(hrs) if hrs else None

        pa, pz = pace_of(a_pts), pace_of(z_pts)
        ha, hz = hr_of_part(a_pts), hr_of_part(z_pts)
        both_hr = ha is not None and hz is not None
        if pa is None or pz is None:
            title = f"{num(lr['km'])} km, lari terjauh dengan detak jantung"
            why = ""
        elif pz - pa >= 15 and both_hr and hz <= ha - 2:
            title = f"40% terakhir: pace melambat {secs(pz - pa)} per km, tapi HR justru turun {ha - hz:.0f} bpm"
            why = "Kemungkinan yang capek kaki, bukan jantung, atau memang sengaja diperlambat. "
        elif pz - pa >= 15 and both_hr:
            title = f"40% terakhir: pace melambat {secs(pz - pa)} per km dan HR naik {hz - ha:.0f} bpm"
            why = "Pola capek klasik: makin pelan tapi jantung makin kerja. "
        elif pz - pa >= 15:
            title = f"40% terakhir: pace melambat {secs(pz - pa)} per km"
            why = ""
        else:
            title = f"Pace stabil sampai akhir ({mmss(pa)} → {mmss(pz)} /km)"
            why = "Pembagian tenaganya rapi. "

        def part(name, p, h):
            bits = ([f"<b>{mmss(p)} /km</b>"] if p is not None else []) + ([f"HR <b>{h:.0f}</b>"] if h is not None else [])
            return f"{name} " + " di ".join(bits) if bits else ""
        parts = [x for x in (part("60% pertama", pa, ha), part("40% terakhir", pz, hz)) if x]
        lead = (f"{num(lr['km'])} km, {tgl(lr['start'])}, mulai dari {lr['place']}. "
                + (", ".join(parts) + ". " if parts else "") + why)
        if sp:
            slow = max(sp, key=lambda s: s["s"])
            lead += f"Km paling lambat: km {slow['km']} ({mmss(slow['s'])}, naik {slow['gain']:.0f} m)."
        else:
            lead += "Belum sampai 1 km penuh, jadi tidak ada waktu per km."
        out.append(section("terjauh", "Lari terjauh", title, lead, chart_longest(lr, sp),
                           table(["#Km", "#Waktu", "#HR", "#Naik"],
                                 [(str(s["km"]), mmss(s["s"]), f"{s['hr']:.0f}" if s["hr"] else "-", f"{s['gain']:.0f} m")
                                  for s in sp]),
                           "Geser kursor di atas grafik untuk membaca ketiga panel di titik yang sama."))

    # 8. load
    prev4 = [wk_km[w] for w in weeks[-5:-1]]
    avg4 = statistics.mean(prev4) if len(prev4) == 4 else 0
    if avg4:
        this = wk_km[weeks[-1]]
        ratio = this / avg4
        zone = next(z for z in LOAD_ZONES if z[0] <= min(ratio, 2.99) < z[1])
        cap = 1.3 * statistics.mean([wk_km[w] for w in weeks[-4:]])
        partial = today.weekday() < 6
        title = f"Minggu ini {num(ratio)}× rata-rata 4 minggu sebelumnya"
        lead = (f'<span class="chip">{zone[4]} {zone[2]}</span> '
                + f"<b>{num(this)} km</b> minggu ini" + (f" (baru sampai {HARI_PANJANG[today.weekday()]})" if partial else "")
                + f" vs rata-rata <b>{num(avg4)} km</b> di 4 minggu sebelumnya. "
                + f"Supaya rasio minggu depan ≤ 1,3×, jaga di sekitar <b>{num(cap, 0)} km</b> atau kurang.")
        out.append(section("beban", "Beban latihan", title, lead, chart_load(ratio),
                           table(["Minggu", "#Km"], [(tgl(w), num(wk_km[w])) for w in weeks[-5:]]),
                           "Rasio beban akut:kronis. Patokan umumnya 0,8–1,3× dianggap aman dan di atas 1,5× rawan cedera, "
                           "tapi ini aturan praktis, bukan hukum pasti."))

    page = f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cerita Lari {esc(span)}</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head>
<body><div class="wrap">
  <p class="eyebrow">Cerita lari · Strava · {esc(span)}</p>
  <h1>{num(km_all)} km dalam {len(summ)} lari sejak {BULAN_PANJANG[SINCE.month]}</h1>
  <p class="sub">Data sinkron {esc(fetched)} · {len(summ)} lari sejak {tgl(SINCE)} {SINCE.year} · {len(runs)} dengan GPX · {len(hr_runs)} dengan detak jantung · grafik dihitung dari setiap titik GPX</p>
  {hero}
  {"".join(out)}
  <footer>REA-STRAVA · STORY · DIBANGKITKAN {now:%Y-%m-%d %H:%M} · SUMBER: data/activities.json + GPX · {merged} UNGGAHAN GANDA DIGABUNG</footer>
</div><div id="tip" hidden></div><script>{JS}</script></body></html>"""
    REPORTS.mkdir(parents=True, exist_ok=True)
    out_file = REPORTS / "story.html"
    out_file.write_text(page)
    print(f"written: {out_file} ({len(page) / 1024:.0f} KB, {len(runs)} runs, {merged} duplicates merged)")


if __name__ == "__main__":
    build()
