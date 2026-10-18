/**
 * Receiver for the strava-reader sheet sync (README step 7): scripts/sheet.py
 * --push POSTs {"token", "v", "spreadsheetId", "tabs": [{name, formats, values, formulas}]}.
 *
 * Each tab is rewritten whole (nothing is ever appended, so a rerun cannot
 * duplicate rows). Values are text, numbers or booleans and never run as
 * formulas: a string starting with = + - @ or ' is written with a leading
 * apostrophe, so it stays literal text (and reads back without it). The only
 * formulas are the ones listed in "formulas" as [row, column, formula]
 * (1-based; those cells are "" in values). The per-column number formats are
 * applied. It answers {"ok": true, "tabs": {name: values}} with every cell
 * read back — cells carrying a date/time number format read back as Date
 * objects, so they are returned as spreadsheet day numbers (days since
 * 1899-12-30), which keeps sheet.py's compare exact and locale-free.
 *
 * Safety: one push at a time (script lock). Every tab is checked before
 * anything is written, then built in a hidden staging tab; the live tabs are
 * only overwritten once every staging tab was written, so a failed push
 * leaves the previous contents in place.
 *
 * One-time setup: Extensions → Apps Script → paste this file, then set the
 * script properties (Project Settings → Script Properties) "TOKEN" to the
 * same value as STRAVA_SHEET_TOKEN in the repo's .env and "SPREADSHEET_ID" to
 * the same value as STRAVA_SHEET_ID (the id in the sheet's URL); a push for
 * any other id is refused. Deploy → New deployment → Web app, execute as me,
 * access Anyone; authorize; copy the /exec URL into .env as STRAVA_SHEET_URL.
 * The token is read from the property only, so it never sits in this file.
 * Editing this file does NOT change what /exec runs: re-paste it, then
 * Deploy → Manage deployments → edit → Version: New version → Deploy; every
 * answer carries "v" so sheet.py can tell which version is live.
 */
const VERSION = 3;  // bumped whenever this file changes; scripts/sheet.py checks it
const STAGING = '__staging_';
const LOCK_WAIT_MS = 120000;

function serial_(d, tz) {
  // Spreadsheet day number of a Date, via its wall clock in tz.
  const p = Utilities.formatDate(d, tz, 'yyyy,MM,dd,HH,mm,ss').split(',').map(Number);
  return (Date.UTC(p[0], p[1] - 1, p[2], p[3], p[4], p[5]) - Date.UTC(1899, 11, 30)) / 86400000;
}

function literal_(v) {
  // A string that Sheets would parse as a formula (or eat the quote of) stays text.
  return typeof v === 'string' && /^[=+\-@']/.test(v) ? "'" + v : v;
}

function check_(tabs) {
  // Throws on the first malformed tab, before anything is written.
  if (!Array.isArray(tabs)) throw new Error('tabs must be a list');
  const seen = {};
  for (const t of tabs) {
    if (!t || typeof t.name !== 'string' || !t.name || t.name.indexOf(STAGING) === 0)
      throw new Error('bad tab name: ' + JSON.stringify(t && t.name));
    if (seen[t.name]) throw new Error('tab sent twice: ' + t.name);
    seen[t.name] = true;
    const values = t.values;
    if (!Array.isArray(values) || !values.length || !Array.isArray(values[0]) || !values[0].length)
      throw new Error(t.name + ': values must be a non-empty matrix');
    const cols = values[0].length;
    values.forEach((row, i) => {
      if (!Array.isArray(row) || row.length !== cols)
        throw new Error(t.name + ' row ' + (i + 1) + ': expected ' + cols + ' cells');
      row.forEach((v, j) => {
        if (!(v === null || ['string', 'number', 'boolean'].indexOf(typeof v) >= 0))
          throw new Error(t.name + ' row ' + (i + 1) + ' column ' + (j + 1) + ': not a plain value');
      });
    });
    for (const letter of Object.keys(t.formats || {}))
      if (!/^[A-Z]$/.test(letter) || letter.charCodeAt(0) - 64 > cols)
        throw new Error(t.name + ': bad format column ' + letter);
    for (const f of t.formulas || []) {
      const ok = Array.isArray(f) && f.length === 3 && Number.isInteger(f[0]) && Number.isInteger(f[1])
        && f[0] >= 1 && f[0] <= values.length && f[1] >= 1 && f[1] <= cols
        && typeof f[2] === 'string' && f[2].charAt(0) === '=' && values[f[0] - 1][f[1] - 1] === '';
      if (!ok) throw new Error(t.name + ': bad formula ' + JSON.stringify(f));
    }
  }
}

function fit_(sh, rows, cols) {
  // Grow the grid so rows x cols fits.
  if (sh.getMaxRows() < rows) sh.insertRowsAfter(sh.getMaxRows(), rows - sh.getMaxRows());
  if (sh.getMaxColumns() < cols) sh.insertColumnsAfter(sh.getMaxColumns(), cols - sh.getMaxColumns());
}

function stage_(ss, t, k) {
  // A hidden tab holding t exactly as the live tab should end up.
  const sh = ss.insertSheet(STAGING + k, ss.getNumSheets());
  sh.hideSheet();
  const values = t.values, rows = values.length, cols = values[0].length;
  fit_(sh, rows, cols);
  sh.getRange(1, 1, rows, cols).setValues(values.map((row) => row.map(literal_)));
  for (const [letter, fmt] of Object.entries(t.formats || {}))
    if (rows > 1) sh.getRange(2, letter.charCodeAt(0) - 64, rows - 1, 1).setNumberFormat(fmt);
  for (const [i, j, f] of t.formulas || []) sh.getRange(i, j).setFormula(f);
  return sh;
}

function commit_(ss, t, staged) {
  // Overwrite the live tab with its staging copy, then clear what lies beyond it.
  const rows = t.values.length, cols = t.values[0].length;
  let sh = ss.getSheetByName(t.name);
  if (!sh) sh = ss.insertSheet(t.name);
  const lastRow = sh.getLastRow(), lastCol = sh.getLastColumn();
  fit_(sh, rows, cols);
  staged.getRange(1, 1, rows, cols).copyTo(sh.getRange(1, 1, rows, cols));
  const wide = Math.max(lastCol, cols);
  if (lastRow > rows) sh.getRange(rows + 1, 1, lastRow - rows, wide).clear();
  if (lastCol > cols) sh.getRange(1, cols + 1, rows, lastCol - cols).clear();
  return sh;
}

function dropStaging_(ss) {
  for (const sh of ss.getSheets()) if (sh.getName().indexOf(STAGING) === 0) ss.deleteSheet(sh);
}

function doPost(e) {
  const respond = (out) =>
    ContentService.createTextOutput(JSON.stringify({ v: VERSION, ...out }))
      .setMimeType(ContentService.MimeType.JSON);
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(LOCK_WAIT_MS)) return respond({ ok: false, error: 'another push is still running' });
  let ss = null;
  try {
    const req = JSON.parse(e.postData.contents);
    const props = PropertiesService.getScriptProperties();
    const want = props.getProperty('TOKEN');
    if (!want || req.token !== want) return respond({ ok: false, error: 'bad token' });
    if (req.v !== VERSION)
      return respond({ ok: false, error: 'sheet.py speaks v' + req.v + ', this web app v' + VERSION });
    const id = props.getProperty('SPREADSHEET_ID');
    if (!id) return respond({ ok: false, error: 'script property SPREADSHEET_ID is not set' });
    if (req.spreadsheetId !== id)
      return respond({ ok: false, error: 'spreadsheetId ' + req.spreadsheetId + ' is not the configured sheet' });
    check_(req.tabs);
    ss = SpreadsheetApp.openById(id);
    const tz = ss.getSpreadsheetTimeZone();
    dropStaging_(ss);                                // leftovers of a push that died mid-way
    const staged = req.tabs.map((t, k) => stage_(ss, t, k));
    SpreadsheetApp.flush();                          // every staging write landed before live is touched
    const tabs = {};
    req.tabs.forEach((t, k) => {
      const sh = commit_(ss, t, staged[k]);
      tabs[t.name] = sh.getRange(1, 1, t.values.length, t.values[0].length).getValues()
        .map((row) => row.map((v) => (v instanceof Date ? serial_(v, tz) : v)));
    });
    dropStaging_(ss);
    return respond({ ok: true, tabs });
  } catch (err) {
    try { if (ss) dropStaging_(ss); } catch (ignored) { /* the next push drops them */ }
    return respond({ ok: false, error: String((err && err.message) || err) });
  } finally {
    lock.releaseLock();
  }
}
