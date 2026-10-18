#!/usr/bin/env python3
"""Sync your Strava data without a subscription.

Endpoints (session-cookie authenticated, traced from the signed-in web app via CDP):
  GET /athlete/training_activities?page=N   JSON summaries (needs X-Requested-With: XMLHttpRequest)
  GET /activities/{id}/export_original      raw device upload (FIT)
  GET /activities/{id}/export_gpx           GPX (GPS + elevation + heart rate)
  GET /activities/{id}/streams              403 paywall (subscription-only) - not needed

Session: a dedicated headless Chrome on the cloned profile (chrome-data/)
keeps the Strava login. Each run syncs its _strava4_session cookie and uses it
for direct HTTP requests. If the session expired, writes data/SESSION_EXPIRED
and exits 2 - run scripts/relogin.sh, log in once, then re-run.
"""
import base64
import glob
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FIT_DIR = DATA / "fit"
GPX_DIR = DATA / "gpx"
LOGS = ROOT / "logs"
SESSION_FILE = DATA / "session.json"
ACTIVITIES_FILE = DATA / "activities.json"
DOWNLOADS_FILE = DATA / "downloads.json"
EXPIRED_FILE = DATA / "SESSION_EXPIRED"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_DATA = ROOT / "chrome-data"
CDP_PORT = 9222
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
BASE = "https://www.strava.com"


class SessionExpired(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def expire(reason):
    EXPIRED_FILE.write_text(
        f"Strava session expired: {reason}\n"
        f"detected_at: {now_iso()}\n"
        "Fix: run scripts/relogin.sh, log into Strava once, then re-run scripts/fetch.py\n")
    print(f"SESSION EXPIRED: {reason}", file=sys.stderr)
    print("Run scripts/relogin.sh, log in once, then re-run.", file=sys.stderr)
    sys.exit(2)


def agent_browser_bin():
    p = shutil.which("agent-browser")
    if p:
        return p
    for pat in ("~/.nvm/versions/node/*/bin/agent-browser",
                "/usr/local/bin/agent-browser", "/opt/homebrew/bin/agent-browser"):
        hits = sorted(glob.glob(str(Path(pat).expanduser())))
        if hits:
            return hits[-1]
    sys.exit("agent-browser CLI not found")


def ab(*args, stdin=None, check=True):
    r = subprocess.run([agent_browser_bin(), *args], input=stdin,
                       capture_output=True, text=True, timeout=180)
    if check and r.returncode != 0:
        raise RuntimeError(f"agent-browser {' '.join(args)} failed: "
                           f"{r.stderr.strip() or r.stdout.strip()}")
    out = r.stdout
    if "\nWall time:" in out:
        out = out.split("\nWall time:")[0]
    if out.startswith("Wall time:"):
        out = ""
    return out.strip()


def cdp_alive():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_browser():
    if cdp_alive():
        return
    subprocess.Popen(
        [CHROME, f"--user-data-dir={USER_DATA}", f"--remote-debugging-port={CDP_PORT}",
         "--no-first-run", "--no-default-browser-check", "--headless=new", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if cdp_alive():
            return
        time.sleep(0.5)
    sys.exit("could not start automation Chrome on CDP 9222")


def sync_session():
    ensure_browser()
    ab("connect", str(CDP_PORT), check=False)
    ab("open", f"{BASE}/dashboard", check=False)
    out = ab("get", "url")
    url = out.splitlines()[0] if out else ""
    if "/login" in url:
        expire("browser session redirected to /login")
    for line in ab("cookies", "get").splitlines():
        if line.strip().startswith("_strava4_session="):
            val = line.strip().split("=", 1)[1]
            cookie = f"_strava4_session={val}"
            SESSION_FILE.write_text(json.dumps({"cookie": cookie, "synced_at": now_iso()}, indent=1))
            return cookie
    expire("no _strava4_session cookie in automation profile")


def http_get(cookie, url, xhr=False):
    headers = {"User-Agent": UA, "Cookie": cookie}
    if xhr:
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def browser_fetch(url, xhr=False):
    """Fetch a URL from inside the logged-in page (Cloudflare-safe fallback)."""
    headers = {"X-Requested-With": "XMLHttpRequest"} if xhr else {}
    js = f"""
(async () => {{
  const r = await fetch({json.dumps(url)}, {{headers: {json.dumps(headers)}}});
  const buf = new Uint8Array(await r.arrayBuffer());
  let bin = '';
  const CH = 0x8000;
  for (let i = 0; i < buf.length; i += CH) bin += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
  return JSON.stringify({{status: r.status, url: r.url, body: btoa(bin)}});
}})()
"""
    out = ab("eval", "--stdin", "--json", stdin=js)
    line = next(l for l in out.splitlines() if l.startswith("{"))
    payload = json.loads(line)
    result = payload["data"]["result"]
    obj = json.loads(result) if isinstance(result, str) else result
    body = base64.b64decode(obj["body"])
    if "/login" in obj.get("url", ""):
        raise SessionExpired(f"browser fetch redirected to login: {url}")
    return obj["status"], body


def looks_blocked(status, body):
    return status in (403, 429, 503) or (status == 200 and b"Just a moment" in body[:4096])


def get(cookie, url, xhr=False):
    """Direct HTTP with in-browser fallback when Cloudflare blocks plain requests."""
    status, body = http_get(cookie, url, xhr=xhr)
    if looks_blocked(status, body):
        status, body = browser_fetch(url, xhr=xhr)
    if status == 401 or b'id="login_form"' in body[:4096]:
        raise SessionExpired(f"auth failure hitting {url} (HTTP {status})")
    return status, body


def fetch_summaries(cookie):
    models, page, total = [], 1, 0
    while True:
        url = (f"{BASE}/athlete/training_activities?keywords=&sport_type="
               f"&new_activity_only=false&page={page}")
        status, body = get(cookie, url, xhr=True)
        if status != 200:
            raise SessionExpired(f"summaries page {page} returned HTTP {status}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise SessionExpired(f"summaries page {page} returned non-JSON (session dead?)")
        batch = data.get("models", [])
        models.extend(batch)
        total = data.get("total", 0)
        print(f"  page {page}: {len(batch)} activities (total {total})")
        if not batch or page * data.get("perPage", 20) >= total:
            break
        page += 1
        time.sleep(0.3)
    seen = {}
    for m in models:
        seen[m["id_str"]] = m
    models = list(seen.values())
    ACTIVITIES_FILE.write_text(json.dumps({"fetched_at": now_iso(), "total": total,
                                           "models": models}, indent=1))
    return models


def download_one(cookie, m):
    aid = m["id_str"]
    rec = {"name": m.get("name"), "date": m.get("start_date"), "fit": None, "gpx": None}
    status, body = get(cookie, f"{BASE}/activities/{aid}/export_original")
    if status == 200 and len(body) > 32 and not body.lstrip()[:15].startswith(b"<!DOCTYPE"):
        (FIT_DIR / f"{aid}.fit").write_bytes(body)
        rec["fit"] = len(body)
    elif status == 404:
        rec["fit"] = "404"
    else:
        rec["fit"] = f"error:{status}"
    status, body = get(cookie, f"{BASE}/activities/{aid}/export_gpx")
    if status == 200 and body.lstrip().startswith((b"<?xml", b"<gpx")):
        (GPX_DIR / f"{aid}.gpx").write_bytes(body)
        rec["gpx"] = len(body)
    elif status == 404:
        rec["gpx"] = "404"
    else:
        rec["gpx"] = f"error:{status}"
    ok = (isinstance(rec["fit"], int) or isinstance(rec["gpx"], int)
          or rec["fit"] == "404" or rec["gpx"] == "404")
    rec["state"] = "ok" if ok else "error"
    return rec


def download_files(cookie, models):
    downloads = json.loads(DOWNLOADS_FILE.read_text()) if DOWNLOADS_FILE.exists() else {}
    todo = [m for m in models
            if downloads.get(m["id_str"], {}).get("state") not in ("ok", "missing")]
    print(f"files to fetch: {len(todo)} of {len(models)} activities")
    for i, m in enumerate(todo, 1):
        try:
            rec = download_one(cookie, m)
        except SessionExpired:
            raise
        except Exception as e:
            rec = {"name": m.get("name"), "state": "error", "error": str(e)[:200]}
        rec["state"] = rec.get("state", "error")
        downloads[m["id_str"]] = rec
        DOWNLOADS_FILE.write_text(json.dumps(downloads, indent=1, sort_keys=True))
        if isinstance(rec.get("fit"), int) and isinstance(rec.get("gpx"), int):
            tag = f"fit {rec['fit'] // 1024} KB + gpx {rec['gpx'] // 1024} KB"
        else:
            tag = f"fit={rec.get('fit')} gpx={rec.get('gpx')} ({rec['state']})"
        print(f"  [{i}/{len(todo)}] {m['start_date']} {m.get('name', '')!r}: {tag}")
        time.sleep(0.4)
    return len(todo)


def main():
    for d in (DATA, FIT_DIR, GPX_DIR, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    if EXPIRED_FILE.exists():
        EXPIRED_FILE.unlink()
    cookie = sync_session()
    print(f"session synced {now_iso()}")
    try:
        models = fetch_summaries(cookie)
        print(f"summaries: {len(models)} activities -> {ACTIVITIES_FILE.relative_to(ROOT)}")
        n = download_files(cookie, models)
        print(f"file sync complete: {n} activities processed")
    except SessionExpired as e:
        expire(str(e))


if __name__ == "__main__":
    main()
