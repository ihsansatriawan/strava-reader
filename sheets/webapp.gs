/**
 * Receiver for the strava-reader sheet sync (README step 7): scripts/sheet.py
 * --push POSTs {"token", "spreadsheetId", "tabs": [{name, formats, values}]}.
 *
 * Each tab is cleared and written whole (nothing is ever appended, so a rerun
 * cannot duplicate rows), values are written raw (no UI parsing), and the
 * per-column number formats are applied. Cells starting with "=" become
 * formulas. It answers {"ok": true, "tabs": {name: values}} with every cell
 * read back — cells carrying a date/time number format read back as Date
 * objects, so they are returned as spreadsheet day numbers (days since
 * 1899-12-30), which keeps sheet.py's compare exact and locale-free.
 *
 * One-time setup: Extensions → Apps Script → paste this file, then set the
 * script property "TOKEN" (Project Settings → Script Properties) to the same
 * value as STRAVA_SHEET_TOKEN in the repo's .env; Deploy → New deployment →
 * Web app, execute as me, access Anyone; authorize; copy the /exec URL into
 * .env as STRAVA_SHEET_URL. The token is read from the property only, so it
 * never sits in this file. Editing this file does NOT change what /exec runs:
 * re-paste it, then Deploy → Manage deployments → edit → Version: New version
 * → Deploy; every answer carries "v" so sheet.py can tell which version is live.
 */
const VERSION = 2;  // bumped whenever this file changes; scripts/sheet.py checks it

function serial_(d, tz) {
  // Spreadsheet day number of a Date, via its wall clock in tz.
  const p = Utilities.formatDate(d, tz, 'yyyy,MM,dd,HH,mm,ss').split(',').map(Number);
  return (Date.UTC(p[0], p[1] - 1, p[2], p[3], p[4], p[5]) - Date.UTC(1899, 11, 30)) / 86400000;
}

function doPost(e) {
  const respond = (out) =>
    ContentService.createTextOutput(JSON.stringify({ v: VERSION, ...out }))
      .setMimeType(ContentService.MimeType.JSON);
  try {
    const req = JSON.parse(e.postData.contents);
    const want = PropertiesService.getScriptProperties().getProperty('TOKEN');
    if (!want || req.token !== want) return respond({ ok: false, error: 'bad token' });
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const tz = ss.getSpreadsheetTimeZone();
    const tabs = {};
    for (const t of req.tabs) {
      let sh = ss.getSheetByName(t.name);
      if (!sh) sh = ss.insertSheet(t.name);
      sh.clear();
      const values = t.values;
      const rows = values.length, cols = values[0].length;
      sh.getRange(1, 1, rows, cols).setValues(values);          // raw, never UI-parsed
      for (const [letter, fmt] of Object.entries(t.formats || {}))
        if (rows > 1) sh.getRange(2, letter.charCodeAt(0) - 64, rows - 1, 1).setNumberFormat(fmt);
      for (let i = 0; i < rows; i++)
        for (let j = 0; j < cols; j++) {
          const v = values[i][j];
          if (typeof v === 'string' && v.charAt(0) === '=') sh.getRange(i + 1, j + 1).setFormula(v);
        }
      tabs[t.name] = sh.getRange(1, 1, rows, cols).getValues()
        .map((row) => row.map((v) => (v instanceof Date ? serial_(v, tz) : v)));
    }
    return respond({ ok: true, tabs });
  } catch (err) {
    return respond({ ok: false, error: String((err && err.message) || err) });
  }
}