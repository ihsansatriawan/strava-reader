#!/usr/bin/env python3
"""Build reports/progress.html - visual progress dashboard from synced Strava data.

Charts (all inline SVG, no JS, design follows the diagram-design style guide):
  1. KPI cards           - totals, pace, HR, records
  2. Weekly volume       - 16 weeks, bars + 4-week rolling average
  3. Pace trend          - per-run pace + 5-run rolling median (inverted axis)
  4. HR efficiency       - avg HR vs pace scatter, colored by month
  5. Distance per run    - the long-run progression
  6. Consistency grid    - weeks x days, run days filled
  7. Insights            - computed, not hand-written
"""
import math
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from analyze import gpx_stats, load, GPX_DIR  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
SINCE = datetime(2026, 6, 1)

INK = "#2d3142"
MUTED = "#4f5d75"
SOFT = "#5a6580"
ACCENT = "#bf4520"
PAPER = "#f5f5f5"
HAIR = "rgba(45,49,66,0.12)"
LINK = "#2e5aa8"
CARD = "#ffffff"

SANS = "'Geist', system-ui, sans-serif"
MONO = "'Geist Mono', ui-monospace, monospace"
SERIF = "'Instrument Serif', serif"

HR_RE = re.compile(r"<gpxtpx:hr>(\d+)</gpxtpx:hr>")


def pace(sec_per_km):
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}"


def load_runs():
    fetched_at, acts, _ = load()  # duplicate phone + watch uploads already merged
    runs = []
    for a in acts:
        if a["type"] != "Run" or a["km"] < 1 or a["trainer"]:
            continue
        if a["start"] < SINCE:
            continue
        p = a["moving_s"] / a["km"]  # s per km
        run = {"date": a["start"], "km": a["km"], "pace": p, "moving_s": a["moving_s"]}
        g = GPX_DIR / f"{a['id']}.gpx"
        if g.exists():
            try:
                st = gpx_stats(g)
                run["hr"] = st["avg_hr"]
            except OSError:
                pass
        runs.append(run)
    runs.sort(key=lambda r: r["date"])
    return runs, fetched_at


def svg(w, h, body):
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="width:100%;display:block" role="img">{body}</svg>')


def txt(x, y, s, size=8, fill=MUTED, family=MONO, anchor="middle", weight=400, extra=""):
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
            f'font-family="{family}" text-anchor="{anchor}" font-weight="{weight}" {extra}>{s}</text>')


# ---------------- charts ----------------

def chart_weekly(runs):
    """Bars: km per ISO week, last 16 weeks + 4-week rolling avg line."""
    monday = datetime.now().date() - timedelta(days=datetime.now().isoweekday() - 1)
    weeks = [monday - timedelta(weeks=i) for i in range(15, -1, -1)]
    km = {w: 0.0 for w in weeks}
    for r in runs:
        wk = r["date"].date() - timedelta(days=r["date"].isoweekday() - 1)
        if wk in km:
            km[wk] += r["km"]
    vals = [km[w] for w in weeks]
    roll = [sum(vals[max(0, i - 3):i + 1]) / len(vals[max(0, i - 3):i + 1]) for i in range(len(vals))]
    top = max(26, int(max(vals) + 2))
    X0, X1, Y0, Y1 = 44, 932, 36, 208
    bw = (X1 - X0) / 16
    body = []
    for gy in range(0, top, 5):
        y = Y1 - gy / top * (Y1 - Y0)
        body.append(f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="{HAIR}" stroke-width="0.8"/>')
        body.append(txt(X0 - 8, y + 3, str(gy), 8, SOFT, anchor="end"))
    for i, w in enumerate(weeks):
        x = X0 + i * bw + 4
        h = vals[i] / top * (Y1 - Y0)
        if h > 0:
            cur = (i == 15)
            fill = "rgba(191,69,32,0.14)" if cur else "rgba(45,49,66,0.14)"
            stroke = ACCENT if cur else "none"
            body.append(f'<rect x="{x:.0f}" y="{Y1 - h:.1f}" width="{bw - 8:.0f}" height="{h:.1f}" rx="3" fill="{fill}"'
                        + (f' stroke="{stroke}" stroke-width="1"' if cur else "") + "/>")
        if i % 2 == 0:
            body.append(txt(x + (bw - 8) / 2, Y1 + 14, f"{w.month}/{w.day}", 8, SOFT))
    pts = []
    for i, rv in enumerate(roll):
        x = X0 + i * bw + bw / 2
        pts.append(f"{x:.1f},{Y1 - rv / top * (Y1 - Y0):.1f}")
    body.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{INK}" stroke-width="1.4"/>')
    body.append(txt(X1, Y0 - 14, "rata-rata 4 minggu", 8, INK, anchor="end"))
    return svg(960, 232, "".join(body))


def chart_pace(runs):
    """Scatter + rolling median. Inverted y: faster = higher."""
    pts = [(i, r["pace"]) for i, r in enumerate(runs)]
    med = []
    for i in range(len(runs)):
        w = pts[max(0, i - 4):i + 1]
        med.append(statistics.median(p[1] for p in w))
    paces = [p[1] for p in pts] + med
    lo, hi = min(paces) - 8, max(paces) + 8
    X0, X1, Y0, Y1 = 44, 932, 36, 200

    def Y(p):
        return Y0 + (p - lo) / (hi - lo) * (Y1 - Y0)

    body = []
    for t in range(int(lo // 30 * 30 + 30), int(hi) + 1, 30):
        y = Y(t)
        body.append(f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="{HAIR}" stroke-width="0.8"/>')
        body.append(txt(X0 - 8, y + 3, pace(t), 8, SOFT, anchor="end"))
    months = {}
    for i, r in enumerate(runs):
        months.setdefault((r["date"].year, r["date"].month), []).append(i)
    for (y_, m), idxs in months.items():
        body.append(txt(X0 + idxs[0] * (X1 - X0) / max(1, len(runs) - 1), Y0 - 12,
                        ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"][m], 8, SOFT))
    step = (X1 - X0) / max(1, len(runs) - 1)
    for i, r in enumerate(runs):
        x = X0 + i * step
        body.append(f'<circle cx="{x:.1f}" cy="{Y(r["pace"]):.1f}" r="3" fill="{MUTED}" fill-opacity="0.55"/>')
    mpts = [f"{X0 + i * step:.1f},{Y(med[i]):.1f}" for i in range(len(runs))]
    body.append(f'<polyline points="{" ".join(mpts)}" fill="none" stroke="{ACCENT}" stroke-width="1.6"/>')
    body.append(txt(X1, Y0 - 12, "median bergerak 5 lari", 8, ACCENT, anchor="end"))
    return svg(960, 224, "".join(body))


def chart_hreff(runs):
    """HR vs pace scatter; faster to the right; color = month ramp."""
    data = [r for r in runs if r.get("hr")]
    if len(data) < 4:
        return svg(960, 200, txt(480, 100, "data HR belum cukup", 10, SOFT, SANS))
    months = sorted({(r["date"].year, r["date"].month) for r in data})
    paces = [r["pace"] for r in data]
    hrs = [r["hr"] for r in data]
    plo, phi = min(paces) - 5, max(paces) + 5
    hlo, hhi = min(hrs) - 5, max(hrs) + 5
    X0, X1, Y0, Y1 = 44, 932, 36, 200
    body = []
    for t in range(int(hlo // 10 * 10 + 10), int(hhi) + 1, 10):
        y = Y1 - (t - hlo) / (hhi - hlo) * (Y1 - Y0)
        body.append(f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="{HAIR}" stroke-width="0.8"/>')
        body.append(txt(X0 - 8, y + 3, str(t), 8, SOFT, anchor="end"))
    for i, r in enumerate(data):
        x = X1 - (r["pace"] - plo) / (phi - plo) * (X1 - X0)
        y = Y1 - (r["hr"] - hlo) / (hhi - hlo) * (Y1 - Y0)
        mi = months.index((r["date"].year, r["date"].month)) / max(1, len(months) - 1)
        op = 0.25 + 0.75 * mi
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{ACCENT}" fill-opacity="{op:.2f}"/>')
    body.append(txt(X0, Y1 + 16, "lambat", 8, SOFT, anchor="start"))
    body.append(txt(X1, Y1 + 16, "cepat", 8, SOFT, anchor="end"))
    body.append(txt((X0 + X1) / 2, Y1 + 16, "pace (menit/km)", 8, SOFT))
    lx = X0
    for i, (y_, m) in enumerate(months):
        op = 0.25 + 0.75 * i / max(1, len(months) - 1)
        lbl = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"][m]
        body.append(f'<circle cx="{lx}" cy="{Y0 - 8}" r="4" fill="{ACCENT}" fill-opacity="{op:.2f}"/>')
        body.append(txt(lx + 8, Y0 - 5, lbl, 8, SOFT, anchor="start"))
        lx += 60
    return svg(960, 224, "".join(body))


def chart_distance(runs):
    """Per-run distance dots; the scale reaches the longest run (at least 12 km)."""
    X0, X1, Y0, Y1 = 44, 932, 36, 200
    top = max(12, math.ceil(max((r["km"] for r in runs), default=0) / 3) * 3)
    body = []
    for gy in range(0, top + 1, 3):
        y = Y1 - gy / top * (Y1 - Y0)
        body.append(f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="{HAIR}" stroke-width="0.8"/>')
        body.append(txt(X0 - 8, y + 3, str(gy), 8, SOFT, anchor="end"))
    step = (X1 - X0) / max(1, len(runs) - 1)
    cutoff = datetime.now() - timedelta(days=28)
    for i, r in enumerate(runs):
        x = X0 + i * step
        y = Y1 - r["km"] / top * (Y1 - Y0)
        recent = r["date"] >= cutoff
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{5 if recent else 3.5}" '
                    f'fill="{ACCENT if recent else MUTED}" fill-opacity="{0.9 if recent else 0.5}"/>')
    body.append(txt(X0, Y0 - 12, "titik besar = 4 minggu terakhir", 8, SOFT, anchor="start"))
    return svg(960, 224, "".join(body))


def chart_calendar(runs):
    """Weeks x days grid, since the first Monday >= SINCE."""
    first = SINCE.date()
    first_monday = first - timedelta(days=first.isoweekday() - 1)
    today = datetime.now().date()
    this_monday = today - timedelta(days=today.isoweekday() - 1)
    weeks = []
    w = first_monday
    while w <= this_monday:
        weeks.append(w)
        w += timedelta(weeks=1)
    km = {}
    for r in runs:
        km[r["date"].date()] = km.get(r["date"].date(), 0) + r["km"]
    days = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
    CW, CH, GAP = 22, 22, 4
    W = 40 + len(weeks) * (CW + GAP) + 8
    H = 24 + 7 * (CH + GAP) + 8
    body = []
    for d in range(7):
        body.append(txt(28, 44 + d * (CH + GAP) + CH / 2 + 3, days[d], 8, SOFT, anchor="end"))
    for wi, wkstart in enumerate(weeks):
        for d in range(7):
            day = wkstart + timedelta(days=d)
            x = 40 + wi * (CW + GAP)
            y = 24 + d * (CH + GAP)
            v = km.get(day, 0)
            if day > today:
                fill, stroke, op = "none", "rgba(45,49,66,0.06)", 1
            elif v == 0:
                fill, stroke, op = "none", HAIR, 1
            elif v < 4:
                fill, stroke, op = "rgba(191,69,32,0.25)", "rgba(191,69,32,0.35)", 1
            elif v < 8:
                fill, stroke, op = "rgba(191,69,32,0.50)", "rgba(191,69,32,0.55)", 1
            else:
                fill, stroke, op = ACCENT, ACCENT, 1
            body.append(f'<rect x="{x}" y="{y}" width="{CW}" height="{CH}" rx="4" fill="{fill}" '
                        f'stroke="{stroke}" stroke-width="1"/>')
            if v >= 8:
                body.append(txt(x + CW / 2, y + CH / 2 + 3, "L", 8, "#f5f5f5", weight=600))
        if wi % 2 == 0:
            body.append(txt(40 + wi * (CW + GAP) + CW / 2, 16, f"{wkstart.month}/{wkstart.day}", 7, SOFT))
    return svg(W, H, "".join(body))


# ---------------- insights ----------------

def insights(runs):
    out = []
    if len(runs) < 6:
        return out
    first4 = statistics.mean(r["pace"] for r in runs[:4])
    last4 = statistics.mean(r["pace"] for r in runs[-4:])
    gain = (first4 - last4) / first4 * 100
    out.append(f"Pace membaik <b>{gain:.0f}%</b> - rata-rata 4 lari pertama {pace(first4)} /km "
               f"menjadi {pace(last4)} /km terakhir.")
    steady = [r for r in runs if 4.5 <= r["km"] <= 5.5 and r.get("hr")]
    best = None
    for i, a in enumerate(steady):
        for b in steady[i + 1:]:
            if abs(a["pace"] - b["pace"]) <= 15 and b["date"] - a["date"] > timedelta(days=14):
                gap = b["date"] - a["date"]
                if best is None or gap > best[0]:
                    best = (gap, a, b)
    if best:
        _, a, b = best
        drop = a["hr"] - b["hr"]
        change = (f"turun <b>{drop:.0f} bpm</b>" if drop > 0 else
                  f"naik <b>{-drop:.0f} bpm</b>" if drop < 0 else "<b>tidak berubah</b>")
        out.append(f"Detak jantung pada lari 5 km dengan pace nyaris sama "
                   f"({pace(a['pace'])} vs {pace(b['pace'])} /km) {change} "
                   f"({a['hr']} &rarr; {b['hr']}) dalam "
                   f"{(b['date'] - a['date']).days} hari - indikator kebugaran aerobik paling jujur.")
    elif len(steady) >= 4:
        e3 = statistics.mean(r["hr"] for r in steady[:3])
        l3 = statistics.mean(r["hr"] for r in steady[-3:])
        out.append(f"HR rata-rata lari 5 km turun <b>{e3 - l3:.0f} bpm</b> ({e3:.0f} &rarr; {l3:.0f}) - "
                   f"sebagian karena pace juga lebih santai, tapi tetap tren yang benar.")
    longest = max(runs, key=lambda r: r["km"])
    lhr = longest.get("hr")
    out.append(f"Long run terjauh: <b>{longest['km']:.1f} km</b> ({longest['date']:%d %b})" +
               (f" dengan HR rata-rata hanya <b>{lhr}</b> - basis aerobik sehat, masih ada ruang "
                f"untuk nambah kecepatan di sesi pendek." if lhr else "."))
    weekly = {}
    for r in runs:
        wk = r["date"].date() - timedelta(days=r["date"].isoweekday() - 1)
        weekly[wk] = weekly.get(wk, 0) + r["km"]
    this_wk = datetime.now().date() - timedelta(days=datetime.now().isoweekday() - 1)
    prev = [weekly.get(this_wk - timedelta(weeks=i), 0) for i in range(1, 5)]
    avg4 = statistics.mean(prev) if prev else 0
    tw = weekly.get(this_wk, 0)
    if avg4 > 0:
        out.append(f"Volume minggu ini <b>{tw:.1f} km</b> ({tw / avg4 * 100 - 100:+.0f}% vs rata-rata 4 minggu "
                   f"sebelumnya {avg4:.1f} km). Kenaikan >10% dua minggu beruntun = risiko cedera - "
                   f"jaga long run tetap pelan.")
    first_wk = min(weekly) - timedelta(days=min(weekly).isoweekday() - 1)
    nweeks = (this_wk - first_wk).days // 7 + 1
    active = sum(1 for w in weekly.values() if w > 0)
    out.append(f"Konsistensi: lari di <b>{active} dari {nweeks}</b> minggu sejak Juni "
               f"({active / nweeks * 100:.0f}%). Kebiasaan sudah terbentuk; sekarang tinggal kadar intensitasnya.")
    return out


# ---------------- page ----------------

CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root { --paper:#f5f5f5; --ink:#2d3142; --muted:#4f5d75; --accent:#bf4520; }
body { font-family:'Geist',system-ui,sans-serif; background:var(--paper); color:var(--ink); padding:2.5rem 1.5rem 4rem; }
.wrap { max-width:1040px; margin:0 auto; }
.eyebrow { font-family:'Geist Mono',ui-monospace,monospace; font-size:0.66rem; letter-spacing:0.18em; text-transform:uppercase; color:var(--muted); margin-bottom:0.5rem; }
h1 { font-family:'Instrument Serif',serif; font-weight:400; font-size:clamp(1.6rem,3vw,2.2rem); letter-spacing:-0.02em; margin-bottom:0.4rem; }
.sub { color:var(--muted); font-size:0.85rem; margin-bottom:2rem; }
.cards { display:grid; grid-template-columns:1.4fr 1fr 1fr 1fr; gap:16px; margin-bottom:24px; }
.card { background:#ffffff; border:1px solid rgba(45,49,66,0.12); border-radius:6px; padding:1.1rem 1.25rem; }
.card .k { font-family:'Geist Mono',monospace; font-size:0.62rem; letter-spacing:0.14em; text-transform:uppercase; color:var(--muted); margin-bottom:0.4rem; }
.card .v { font-family:'Instrument Serif',serif; font-size:1.7rem; line-height:1; }
.card .v small { font-family:'Geist',sans-serif; font-size:0.72rem; color:var(--muted); display:block; margin-top:0.35rem; }
.card.accent { border-color:rgba(191,69,32,0.45); }
.card.accent .v { color:var(--accent); }
.sec { background:#ffffff; border:1px solid rgba(45,49,66,0.12); border-radius:6px; padding:1.25rem 1.4rem 0.9rem; margin-bottom:20px; overflow-x:auto; }
.sec h3 { font-size:0.8rem; font-weight:600; margin-bottom:1rem; }
.sec h3 span { font-family:'Geist Mono',monospace; font-size:0.62rem; color:var(--muted); font-weight:400; letter-spacing:0.08em; margin-left:8px; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:20px; align-items:start; }
@media (max-width:860px){ .two{grid-template-columns:1fr;} .cards{grid-template-columns:1fr 1fr;} }
.insights { background:#ffffff; border:1px solid rgba(45,49,66,0.12); border-radius:6px; padding:1.5rem 1.6rem; margin-bottom:20px; }
.insights h2 { font-family:'Instrument Serif',serif; font-weight:400; font-size:1.3rem; margin-bottom:1rem; }
.insights ul { list-style:none; }
.insights li { font-size:0.86rem; line-height:1.55; color:#2d3142; padding-left:1.3rem; position:relative; margin-bottom:0.75rem; }
.insights li::before { content:""; position:absolute; left:0; top:0.42rem; width:8px; height:8px; border-radius:50%; background:rgba(191,69,32,0.5); }
.insights b { color:#bf4520; font-weight:600; }
footer { font-family:'Geist Mono',monospace; font-size:0.62rem; color:var(--muted); letter-spacing:0.1em; border-top:1px solid rgba(45,49,66,0.12); padding-top:1rem; }
"""


def build(runs, fetched):
    now = datetime.now()
    with_h = [r for r in runs if r.get("hr")]
    monthly = {}
    for r in runs:
        k = (r["date"].year, r["date"].month)
        d = monthly.setdefault(k, [0.0, 0])
        d[0] += r["km"]
        d[1] += 1
    cur_m = monthly.get((now.year, now.month), [0, 0])
    prev_m = monthly.get(((now - timedelta(days=now.day)).year, (now - timedelta(days=now.day)).month), [0, 0])
    last4p = statistics.mean(r["pace"] for r in runs[-4:])
    longest = max(runs, key=lambda r: r["km"])
    wk_km = {}
    for r in runs:
        wk = r["date"].date() - timedelta(days=r["date"].isoweekday() - 1)
        wk_km[wk] = wk_km.get(wk, 0) + r["km"]
    delta = f"{(cur_m[0] / prev_m[0] - 1) * 100:+.0f}% vs bulan lalu" if prev_m[0] else "bulan pertama"

    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">Total sejak Juni</div>
        <div class="v">{sum(r['km'] for r in runs):.0f} km<small>{len(runs)} lari · {sum(r['moving_s'] for r in runs) / 3600:.0f} jam</small></div></div>
      <div class="card"><div class="k">Bulan ini</div>
        <div class="v">{cur_m[0]:.1f} km<small>{delta}</small></div></div>
      <div class="card accent"><div class="k">Pace 4 lari terakhir</div>
        <div class="v">{pace(last4p)}<small>menit per km</small></div></div>
      <div class="card"><div class="k">Lari terjauh</div>
        <div class="v">{longest['km']:.1f} km<small>{longest['date']:%d %b} · HR {longest.get('hr', '-')}</small></div></div>
    </div>"""

    ins = "".join(f"<li>{i}</li>" for i in insights(runs))
    page = f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Progress Strava</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head>
<body><div class="wrap">
  <p class="eyebrow">Progress Dashboard · Strava</p>
  <h1>Empat bulan jadi pelari</h1>
  <p class="sub">Data sinkron {fetched} · {len(runs)} lari sejak Juni 2026 · {len(with_h)} dengan data detak jantung</p>
  {cards}
  <div class="insights"><h2>Yang paling penting dari datamu</h2><ul>{ins}</ul></div>
  <div class="sec"><h3>Volume mingguan <span>16 MINGGU · BATANG = KM/MINGGU · GARIS = RATA-RATA 4 MINGGU</span></h3>{chart_weekly(runs)}</div>
  <div class="sec"><h3>Tren pace <span>SETIAP TITIK = SATU LARI · GARIS MERAH = MEDIAN BERGERAK · MAKIN ATAS = MAKIN CEPAT</span></h3>{chart_pace(runs)}</div>
  <div class="sec"><h3>Efisiensi jantung <span>HR RATA-RATA VS PACE · WARNA MUDA = JULI, PEKAT = SEKARANG · TURUN = LEBIH BUGAR</span></h3>{chart_hreff(runs)}</div>
  <div class="two">
    <div class="sec"><h3>Jarak per lari <span>GELOMBANG MENUJU LONG RUN</span></h3>{chart_distance(runs)}</div>
    <div class="sec"><h3>Kalender konsistensi <span>L = LONG RUN ≥ 8 KM</span></h3>{chart_calendar(runs)}</div>
  </div>
  <footer>REA-STAVA · DIBANGKITKAN {now:%Y-%m-%d %H:%M} · SUMBER: data/activities.json + GPX</footer>
</div></body></html>"""
    out = REPORTS / "progress.html"
    REPORTS.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"written: {out}")
    for i in insights(runs):
        print(" -", re.sub(r"<[^>]+>", "", i).replace("&rarr;", "\u2192"))


if __name__ == "__main__":
    r, f = load_runs()
    if len(r) < 3:
        raise SystemExit("not enough runs yet - run scripts/fetch.py first")
    build(r, f)
