#!/usr/bin/env python3
"""Mirror the synced data into a Google Sheet, one tab per view.

Every run rebuilds every tab from data/ and rewrites it whole, so the sheet
can never collect duplicate rows: each activity appears once in Aktivitas /
Data alat (FIT), each kept run once in Analisis lari, and rerunning changes
nothing. The rows go to reports/sheet.json; --push POSTs them to the small
Apps Script web app bound to the sheet (sheets/webapp.gs), which writes
every tab to a hidden staging copy first and only then overwrites the live
tabs (one push at a time) — a plain HTTPS POST to Apps Script, no LLM in the
loop, so a sync costs no model tokens. The web app answers with every cell
it wrote, which --push then checks against the data, cell by cell. One-time
setup is in README step 7; .env:

    STRAVA_SHEET_ID=<the id in the sheet's URL, same as the SPREADSHEET_ID script property>
    STRAVA_SHEET_URL=<the web app /exec URL>
    STRAVA_SHEET_TOKEN=<the shared secret, same as the TOKEN script property>

Values go out raw (numbers, strings, booleans) with a number format per
column, so the sheet can sort and sum them. Text is always text: a name like
"=1+1" stays literal; the only formulas are the ones built here (Formula),
sent apart from the values. Stdlib only.
"""
import json
import re
import statistics
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fit  # noqa: E402
import story  # noqa: E402
from analyze import (ACTIVITIES_FILE, FIT_DIR, GPX_DIR, REPORTS, ROOT,  # noqa: E402
                     dedupe, gpx_has_hr, read_activities)
from config import DATA_SINCE, in_scope  # noqa: E402

OUT_FILE = REPORTS / "sheet.json"
READBACK_FILE = REPORTS / "sheet_readback.json"
ENV_FILE = ROOT / ".env"
DECOUPLE_MIN_S = 1800             # decoupling needs a run of at least 30 minutes
RUN_CAD = 50                      # one-foot cadence above this = running
CAD_RE = re.compile(r"<gpxtpx:cad>(\d+)</gpxtpx:cad>")
DEVICE_RE = re.compile(r"^\S+ \d{4}-\d\d-\d\d \d\d:\d\d:\d\d (.+)$")
SOURCES = {fit.STRAVA_APP: "Strava app (HP)", 255: "Huawei (jam)"}
SUB_SPORTS = {1: "treadmill", 2: "street", 3: "trail", 4: "track", 27: "indoor_walking",
              30: "casual_walking", 31: "speed_walking"}
EPOCH = datetime(1899, 12, 30)    # spreadsheet day 0

F_ID, F_DT, F_DATE, F_HMS, F_MS, F_PCT = "0", "yyyy-mm-dd hh:mm", "yyyy-mm-dd", "[h]:mm:ss", "[m]:ss", "0%"
WEBAPP_V = 3                       # VERSION in sheets/webapp.gs; bump both together


class Formula(str):
    """A cell the web app writes as a formula; shows is what it reads back as."""

    def __new__(cls, text, shows):
        f = super().__new__(cls, text)
        f.shows = shows
        return f


def serial(d):
    """Spreadsheet day number, to the minute."""
    d = datetime.combine(d, datetime.min.time()) if isinstance(d, date) and not isinstance(d, datetime) else d
    return (d.replace(second=0, microsecond=0) - EPOCH).total_seconds() / 86400


def dur(seconds):
    """Whole seconds as a fraction of a day (what a [h]:mm:ss cell holds), or blank."""
    return "" if seconds is None else round(seconds) / 86400


def opt(x, nd=None):
    if x is None:
        return ""
    return round(x, nd) if nd is not None else x


def session(path):
    """FIT session message fields by number, or {} without a usable FIT file."""
    if not path.exists():
        return {}, {}
    try:
        msgs = fit.messages(path.read_bytes())
    except fit.FitError:
        return {}, {}
    return (next((v for g, v in msgs if g == 0), {}), next((v for g, v in msgs if g == 18), {}))


def fastest(pts, meters):
    """Fewest seconds any stretch of the track took to cover meters, or None."""
    best, j = None, 0
    for i in range(len(pts)):
        while j < len(pts) and pts[j]["d"] - pts[i]["d"] < meters:
            j += 1
        if j == len(pts):
            break
        t = pts[j]["s"] - pts[i]["s"]
        best = t if best is None or t < best else best
    return best


def split_parts(r):
    """First 60% / last 40% of the distance: (pace s/km, mean HR) of each.

    A part without distance or time (a short or sparse track) has pace None;
    a part without HR samples has HR None."""
    pts, total = r["pts"], r["pts"][-1]["d"]
    if total <= 0:
        return None, None, None, None
    k = next(i for i, p in enumerate(pts) if p["d"] >= 0.6 * total)
    a, b = pts[:k], pts[k:]

    def pace(ds, s):
        return s / (ds / 1000) if ds > 0 and s > 0 else None
    first = pace(pts[k]["d"] - pts[0]["d"], pts[k]["s"] - pts[0]["s"])
    last = pace(total - pts[k]["d"], pts[-1]["s"] - pts[k]["s"])
    hr = [statistics.mean(h) if h else None for h in ([p["hr"] for p in a if p["hr"]],
                                                       [p["hr"] for p in b if p["hr"]])]
    return first, last, hr[0], hr[1]


def decoupling(r):
    """Aerobic decoupling: efficiency (speed / mean HR) of the first half of the
    elapsed time over the second half, minus 1. None for short or HR-less runs,
    and when either half has no HR, distance or time."""
    pts = r["pts"]
    if not r["has_hr"] or r["total_s"] < DECOUPLE_MIN_S:
        return None
    mid = pts[0]["s"] + (pts[-1]["s"] - pts[0]["s"]) / 2
    k = next(i for i, p in enumerate(pts) if p["s"] >= mid)

    def ef(x):
        hrs = [p["hr"] for p in x if p["hr"]]
        dt, dd = x[-1]["s"] - x[0]["s"], x[-1]["d"] - x[0]["d"]
        return (dd / dt) / statistics.mean(hrs) if hrs and dt > 0 and dd > 0 else None
    a, b = ef(pts[:k + 1]), ef(pts[k:])
    return a / b - 1 if a and b else None


def cadence(aid):
    """Mean running cadence in steps/min (GPX stores one foot), or None.

    Samples at or below RUN_CAD (walking, standing at a light) are left out.
    """
    c = [int(v) for v in CAD_RE.findall((GPX_DIR / f"{aid}.gpx").read_text(errors="replace")) if int(v) > RUN_CAD]
    return round(statistics.mean(c) * 2) if c else None


def build(now=None):
    now = now or datetime.now()
    raw = json.loads(ACTIVITIES_FILE.read_text())
    models = {m["id_str"]: m for m in in_scope(raw["models"])}
    fetched_at, acts = read_activities()
    acts.sort(key=lambda a: (a["start"], int(a["id"])))
    has_hr = {}

    def hr_of(a):
        if a["id"] not in has_hr:
            has_hr[a["id"]] = gpx_has_hr(GPX_DIR / f"{a['id']}.gpx")
        return has_hr[a["id"]]

    kept, dup_of = dedupe(acts, has_hr=hr_of)
    merged = len(dup_of)
    by_id = {a["id"]: a for a in acts}

    # Aktivitas + Data alat (FIT): every activity, duplicates included and marked.
    aktivitas, alat = [], []
    for a in acts:
        m = models[a["id"]]
        file_id, s = session(FIT_DIR / f"{a['id']}.fit")
        man = file_id.get(1)
        dm = DEVICE_RE.match(m.get("description") or "")
        device = dm[1] if dm else SOURCES.get(man, "")
        moving = round(a["moving_s"])
        aktivitas.append([
            int(a["id"]), a["name"], a["type"], serial(a["start"]), a["start"].strftime("%a"),
            round(a["km"], 2), dur(moving), dur(a["elapsed_s"]), dur(max(0, a["elapsed_s"] - moving)),
            dur(moving / a["km"]) if a["km"] else "", round(a["elev"], 1),
            a["trainer"], a["commute"], m["private"], m["visibility"], m["has_latlng"],
            m["bike_id"] or "", device, m["selected_tag_type"] or "", dup_of.get(a["id"], ""),
            Formula(f'=HYPERLINK("https://www.strava.com/activities/"&A{len(aktivitas) + 2},"buka")', "buka")])
        if not s:
            continue
        strava_km = a.get("strava_km", a["km"])
        speed = s.get(124, s.get(14))
        vmax = s.get(125, s.get(15))
        alat.append([
            int(a["id"]), serial(a["start"]), a["type"], SOURCES.get(man, f"pabrikan {man}"),
            round(a["km"], 2), round(strava_km, 2), round(strava_km / a["km"] - 1, 2) if a["km"] else "",
            dur(s[7] / 1000) if 7 in s else "", dur(s[8] / 1000) if 8 in s else "",
            opt(s.get(11)), opt(s.get(16)), opt(s.get(17)),
            round(speed * 3.6 / 1000, 1) if speed else "", round(vmax * 3.6 / 1000, 1) if vmax else "",
            opt(s.get(18)), opt(s.get(19)), opt(s.get(20)), opt(s.get(21)), opt(s.get(22)), opt(s.get(23)),
            opt(s.get(26)), SUB_SPORTS.get(s.get(6), ""), opt(s.get(10))])

    # Analisis lari + Split per km: the runs story.html reads, duplicates merged;
    # a run whose GPX holds no usable track (story.analyse -> None) has no row.
    outdoor = [a for a in kept if a["start"] >= story.SINCE and a["type"] == "Run" and not a["trainer"]]
    runs = sorted((r for r in (story.analyse(a) for a in outdoor if (GPX_DIR / f"{a['id']}.gpx").exists()) if r),
                  key=lambda r: r["start"])
    phone_km = {k: by_id[d]["km"] for d, k in dup_of.items()}
    analisis, splits = [], []
    for r in runs:
        hr = r["has_hr"]
        zones = sum(r["zones"]) or 1
        p1, p2, h1, h2 = split_parts(r)
        dec = decoupling(r)
        analisis.append([
            int(r["id"]), serial(r["start"]), r["place"], round(r.get("strava_km", r["km"]), 2), round(r["km"], 2),
            round(phone_km[r["id"]], 2) if r["id"] in phone_km else "", round(r["walk_pct"] / 100, 2),
            dur(r["pace_hr"]), round(r["avg_hr"]) if hr else "",
            max(p["hr"] for p in r["pts"] if p["hr"]) if hr else "",
            *[round(z / zones, 2) if hr else "" for z in r["zones"]],
            "" if r["retimed"] else dur(fastest(r["pts"], 1000)),
            "" if r["retimed"] else dur(fastest(r["pts"], 5000)),
            dur(p1), dur(p2), opt(h1 and round(h1)), opt(h2 and round(h2)),
            "" if dec is None else round(dec, 2), opt(cadence(r["id"]) if hr else None),
            r["start"].hour, r["start"].hour < 12])
        for sp in story.splits(r["pts"]):
            splits.append([int(r["id"]), serial(r["start"].date()), sp["km"], dur(sp["s"]),
                           "" if sp["hr"] is None else round(sp["hr"]), round(sp["gain"], 1)])

    # Mingguan: every kept outdoor run per Monday-week (GPX or not), with the
    # acute:chronic ratio.
    weeks, w = [], story.monday(story.SINCE)
    while w <= story.monday(now):
        weeks.append(w)
        w += timedelta(days=7)
    wk = {w: [] for w in weeks}
    for a in outdoor:
        wk[story.monday(a["start"])].append(a)
    km = [sum(a["km"] for a in wk[w]) for w in weeks]
    mingguan = []
    for i, w in enumerate(weeks):
        row = [serial(w), len(wk[w]), round(km[i], 1), dur(sum(r["moving_s"] for r in wk[w])),
               round(sum(r["elev"] for r in wk[w]))]
        if i >= 4:
            avg = sum(km[i - 4:i]) / 4
            row += [round(avg, 1), round(km[i] / avg, 2) if avg else ""]
        mingguan.append(row + [""] * (7 - len(row)))

    since = f"{DATA_SINCE:%Y-%m-%d}"
    catatan = [
        ["Sumber", f"Strava, sinkron {fetched_at}. activities.json + file FIT + GPX, hanya aktivitas sejak {since}. "
                   f"Diperbarui {now:%Y-%m-%d %H:%M}. Semua tab ditulis ulang utuh tiap sinkron, jadi tidak ada baris ganda."],
        ["Aktivitas", f"Semua {len(acts)} aktivitas sejak {since}, termasuk {merged} unggahan ganda "
                      "(kolom duplikat_dari = id versi yang dipakai). Filter duplikat_dari kosong untuk total yang benar."],
        ["Data alat (FIT)", "Ringkasan yang ditulis perangkat sendiri. Huawei: kalori, HR rata-rata, kecepatan. "
                            "Strava app di HP: naik/turun, lap, langkah."],
        ["Jarak", "jarak_km dan waktu_bergerak di tab Aktivitas memakai angka jam sendiri (file FIT) untuk aktivitas yang "
                  "direkam jam; waktu bergerak dihitung dari deret kecepatan jam (>= 1 m/s untuk lari). jarak_strava_km = "
                  "hitungan ulang Strava dari GPS, yang kependekan saat GPS jam macet; lihat selisih_strava_vs_alat."],
        ["Analisis lari", f"Lari sejak {story.SINCE:%Y-%m-%d}, setelah duplikat digabung. Jalan/berhenti = potongan 10 detik "
                          "lebih lambat dari 12:00/km (GPS; memakai kecepatan jam kalau jarak GPS meleset > 3% dari jam). "
                          "Pace di HR 135-145 setelah 5 menit pertama, minimal 5 menit di rentang itu. Rentang HR dipatok dari "
                          "HR tertinggi tercatat, bukan HR maks asli. 1km/5km tercepat dikosongkan untuk lari yang GPS jamnya "
                          "macet: per km-nya cuma perkiraan dari kecepatan jam. Decoupling = efisiensi (kecepatan / HR rata-rata) "
                          "paruh pertama waktu dibanding paruh kedua, minus 1; hanya untuk lari >= 30 menit."],
        ["Split per km", "Waktu per km dari GPS jam/HP, termasuk berhenti. Untuk lari yang GPS jamnya macet, dari kecepatan "
                         "jam (perkiraan, bisa meleset sekitar 1 menit per km)."],
        ["Mingguan", "Semua lari luar ruangan setelah duplikat digabung, ada GPX atau tidak. Rasio beban = km minggu "
                     "itu dibagi rata-rata 4 minggu sebelumnya. Patokan umum 0,8-1,3 aman, di atas 1,5 rawan."],
        ["Privasi", "Sheet ini tidak berisi koordinat GPS atau gambar peta. Tetap jangan dibagikan sembarangan."],
    ]

    ids = [r[0] for r in aktivitas]
    assert len(ids) == len(set(ids)), "duplicate activity_id in Aktivitas"
    assert len({r[0] for r in analisis}) == len(analisis), "duplicate activity_id in Analisis lari"
    tab = lambda name, header, rows, formats: {"name": name, "header": header, "rows": rows, "formats": formats}  # noqa: E731
    return [
        tab("Aktivitas", ["activity_id", "nama", "olahraga", "mulai", "hari", "jarak_km", "waktu_bergerak", "waktu_total",
                          "waktu_berhenti", "pace_per_km", "elevasi_naik_m", "indoor", "commute", "privat", "visibilitas",
                          "ada_gps", "sepeda_id", "perangkat", "tag_strava", "duplikat_dari", "link"], aktivitas,
            {"A": F_ID, "D": F_DT, "G": F_HMS, "H": F_HMS, "I": F_HMS, "J": F_MS, "T": F_ID}),
        tab("Data alat (FIT)", ["activity_id", "mulai", "olahraga", "sumber_file", "jarak_alat_km", "jarak_strava_km",
                                "selisih_strava_vs_alat", "waktu_total", "waktu_timer", "kalori", "hr_rata2", "hr_maks",
                                "kecepatan_rata2_kmh", "kecepatan_maks_kmh", "cadence_rata2", "cadence_maks", "power_rata2_w",
                                "power_maks_w", "naik_m", "turun_m", "jumlah_lap", "sub_sport", "langkah_kayuhan"], alat,
            {"A": F_ID, "B": F_DT, "G": F_PCT, "H": F_HMS, "I": F_HMS}),
        tab("Analisis lari", ["activity_id", "mulai", "tempat", "jarak_strava_km", "jarak_alat_km", "jarak_versi_hp_km",
                              "jalan_berhenti", "pace_di_hr_135_145", "hr_rata2", "hr_maks", "hr_bawah125", "hr_125_139",
                              "hr_140_154", "hr_155_164", "hr_atas165", "1km_tercepat", "5km_tercepat", "pace_60pct_awal",
                              "pace_40pct_akhir", "hr_60pct_awal", "hr_40pct_akhir", "decoupling", "cadence_spm",
                              "jam_mulai", "pagi"], analisis,
            {"A": F_ID, "B": F_DT, "G": F_PCT, "H": F_MS, "K": F_PCT, "L": F_PCT, "M": F_PCT, "N": F_PCT, "O": F_PCT,
             "P": F_MS, "Q": F_HMS, "R": F_MS, "S": F_MS, "V": F_PCT}),
        tab("Split per km", ["activity_id", "tanggal", "km_ke", "waktu", "hr_rata2", "naik_m"], splits,
            {"A": F_ID, "B": F_DATE, "D": F_MS}),
        tab("Mingguan", ["minggu_mulai", "jumlah_lari", "km", "waktu_bergerak", "elevasi_naik_m",
                         "rata2_4_minggu_sebelumnya_km", "rasio_beban"], mingguan, {"A": F_DATE, "D": F_HMS}),
        tab("Catatan", ["bagian", "keterangan"], catatan, {}),
    ]


def env():
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def col(n):
    return chr(ord("A") + n - 1)


def payload(tabs, sheet_id):
    """reports/sheet.json, and what --push POSTs: one entry per tab with the
    raw values (numbers, strings, booleans — text is written as text, never
    run as a formula), the per-column number formats the web app applies,
    and the formula cells apart as [row, column, formula] (1-based, header =
    row 1), which hold "" in values."""
    out = []
    for t in tabs:
        formulas = [[i + 2, j + 1, str(v)] for i, r in enumerate(t["rows"])
                    for j, v in enumerate(r) if isinstance(v, Formula)]
        values = [list(t["header"])] + [["" if isinstance(v, Formula) else v for v in r] for r in t["rows"]]
        out.append({"name": t["name"], "formats": t["formats"], "values": values, "formulas": formulas})
    return {"spreadsheetId": sheet_id, "tabs": out}


def shown(v):
    """What the web app reads back for a cell: the raw value, except a
    Formula (reads back as its computed value) and an all-digit string,
    which Apps Script writes as a number."""
    if isinstance(v, Formula):
        return v.shows
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return v


def check(tabs, readback):
    """Differences between what each tab should hold and what the web app read back.

    Date/time-formatted columns are compared to the second (the sheet's Date
    round-trip can wobble by one); everything else exactly."""
    def trim(r):
        r = list(r)
        while r and r[-1] == "":
            r.pop()
        return r

    def same(a, b, fmt):
        if (isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool)
                and not isinstance(b, bool) and fmt in (F_DT, F_DATE, F_HMS, F_MS)):
            return abs(a - b) * 86400 <= 3
        return a == b

    problems = []
    for t in tabs:
        fmts = t["formats"]
        want = [t["header"]] + [[shown(v) for v in r] for r in t["rows"]]
        got = readback.get(t["name"])
        if got is None:
            problems.append(f"{t['name']}: not read back")
            continue
        got = [trim(r) for r in got]
        while got and not got[-1]:
            got.pop()
        if len(got) != len(want):
            problems.append(f"{t['name']}: {len(got)} rows in the sheet, expected {len(want)}")
        for i, (w, g) in enumerate(zip(want, got)):
            w, g = trim(w), list(g)
            if len(w) != len(g):
                problems.append(f"{t['name']} row {i + 1}: expected {w}, got {g}")
            else:
                bad = [col(j + 1) for j, (a, b) in enumerate(zip(w, g)) if not same(a, b, fmts.get(col(j + 1)))]
                if bad:
                    problems.append(f"{t['name']} row {i + 1}: column {','.join(bad)} expected {w}, got {g}")
    return problems


def push(tabs, sheet_id):
    """POST every tab to the web app bound to the sheet (sheets/webapp.gs),
    which stages and then rewrites each one whole, then check what the sheet
    reads back against the data, cell by cell."""
    e = env()
    url, token = e.get("STRAVA_SHEET_URL"), e.get("STRAVA_SHEET_TOKEN")
    if not url or not token:
        sys.exit(f"STRAVA_SHEET_URL / STRAVA_SHEET_TOKEN missing in {ENV_FILE} (one-time setup, README step 7)")
    body = json.dumps({"token": token, "v": WEBAPP_V, **payload(tabs, sheet_id)}, ensure_ascii=False).encode()
    out, err = None, ""
    for _ in (1, 2):                    # one retry, like the run before it
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                out = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as exc:
            err = f"HTTP {exc.code}: " + exc.read(200).decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            err = str(exc.reason)
    if out is None:
        sys.exit(f"push failed: {url} ({err})")
    if out.get("v") != WEBAPP_V:
        sys.exit(f"push failed: the web app at {url} runs an old version (answers v="
                  f"{out.get('v')}, expected {WEBAPP_V}) - re-paste sheets/webapp.gs in Apps Script, "
                  "then Deploy -> Manage deployments -> edit this deployment -> Version: New version -> Deploy "
                  "(saving the script alone does not change /exec)")
    if not out.get("ok"):
        sys.exit(f"push failed: {out.get('error')}")
    READBACK_FILE.write_text(json.dumps(out["tabs"], ensure_ascii=False, indent=1) + "\n")
    problems = check(tabs, out["tabs"])
    for p in problems[:20]:
        print("MISMATCH", p)
    if problems:
        sys.exit(f"sheet check failed: {len(problems)} problem(s)")
    print("sheet check ok: " + ", ".join(f"{t['name']} {len(t['rows'])}" for t in tabs))


def main():
    tabs = build()
    sheet_id = env().get("STRAVA_SHEET_ID")
    if not sheet_id:
        sys.exit(f"STRAVA_SHEET_ID missing in {ENV_FILE}")
    REPORTS.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload(tabs, sheet_id), ensure_ascii=False, indent=1) + "\n")
    print(f"written: {OUT_FILE} (" + ", ".join(f"{t['name']} {len(t['rows'])}" for t in tabs) + ")")
    if "--push" in sys.argv:
        push(tabs, sheet_id)


if __name__ == "__main__":
    main()
