# Code Review — strava-reader

Date: 2026-10-18 · Reviewer: omp agent (`gpt-astra`) via herdr · Read-only: no files modified.

Scope: `scripts/config.py`, `scripts/fetch.py`, `scripts/fit.py`, `scripts/analyze.py`,
`scripts/progress.py`, `scripts/story.py`, `scripts/sheet.py`, `scripts/sync.sh`,
`scripts/relogin.sh`, `sheets/webapp.gs`, `tests/`.

Every finding below was reproduced read-only during the review session (in-memory fixtures,
mocked services, or isolated subprocesses — no repo files were touched). Ranked by severity;
`concern` items are real correctness bugs reachable by plausible inputs, `nit` is cosmetic
incorrectness. File/line references are to the code at commit `0cad9a2`.

---

## 1. concern — Concurrent pushes can leave stale rows while both report success
`sheets/webapp.gs:41-55`

The clear/write/readback sequence has no lock. Reproduced with interleaved requests: a
smaller update clears, a larger update completes, then the smaller update writes its
prefix. Surplus rows survive, but readback covers only the smaller request's rectangle —
both requests return `ok: true`. Serialize the entire update/readback operation with a
script lock (`LockService.getScriptLock()`).

## 2. concern — Failed replacement destroys the previous sheet contents
`sheets/webapp.gs:44-47`

`clear()` runs before replacement validation or writing. An injected `setValues()` failure
returned an error with the previously populated tab now empty; failures on later tabs also
leave mixed snapshots (some tabs new, some cleared). Validate every matrix first and stage
replacements before clearing live data.

## 3. concern — Activity names are executed as spreadsheet formulas
`scripts/sheet.py:165`; `sheets/webapp.gs:47-53`

Names are copied verbatim, and every string beginning with `=` is treated as executable. A
name of `=1+1` reaches `setFormula()` rather than remaining literal text. Separate
explicitly generated hyperlink formulas from activity/place/device text, and escape or
prefix literal strings before writing.

## 4. concern — Partial download failures become permanently non-retryable
`scripts/fetch.py:233-242`

An activity is marked `ok` when *either* export succeeds or returns 404, even if the other
fails. Reproduced `original=404` + `GPX=500`: state becomes `ok`, and the next sync makes
zero download attempts — the GPX is never retried. Track completion per export; only stop
retrying when both have terminal outcomes (success or 404).

## 5. concern — A truncated FIT can indefinitely block report generation
`scripts/fit.py:26-53`; `scripts/analyze.py:123-124`

The parser trusts the declared payload length and reads fields without bounds checks. A
40-byte truncated FIT raised `struct.error`; `read_activities()` does not isolate that
failure, so one bad file aborts the whole report. The same response also passes the
fetcher's original-download acceptance check. Validate record boundaries, fall back to
summary data for invalid exports, and keep invalid downloads retryable.

## 6. concern — Scheduler exit status hides downstream failures
`scripts/sync.sh:12-18`

Only `fetch_rc` is recorded, and the final successful `echo` determines the wrapper's exit
status. Exercised all five stages failing (with `PY=false`): wrapper still exited 0.
Preserve the intentional continue-after-fetch-failure, but collect stage statuses and exit
nonzero when stages fail.

## 7. concern — Four qualifying HR runs can crash story generation
`scripts/story.py:945-954`

`len(paced) >= 4` enters the section, but monthly groups require at least two runs per
month. One qualifying run in each of four months leaves `med_m=[]`; indexing `med_m[0]`
raises `IndexError` at line 954. Guard the eligible monthly groups before selecting
comparison endpoints.

## 8. concern — Partial HR coverage aborts both report paths
`scripts/story.py:1082-1083`; `scripts/sheet.py:119-126`

`has_hr` means *any* sample has HR, but these calculations require HR in both compared
portions. Reproduced sensor dropout in the last 40% crashing the story, and first-half-only
HR crashing `decoupling()`, both with `StatisticsError` (mean of empty). Check each
segment independently and omit unavailable comparisons.

## 9. concern — Missing elevation crashes an otherwise usable HR report
`scripts/story.py:618-623`

Track parsing explicitly allows `ele=None`, but the longest-run chart calls `min()`/`max()`
after filtering all missing-elevation samples. Reproduced `ValueError` with a valid
distance/HR track lacking elevation. Omit the elevation panel when that channel is absent.

## 10. concern — Short or sparse tracks abort entire exports
`scripts/story.py:1073-1093`; `scripts/sheet.py:105-109`

Three normally-sampled HR runs below 1 km reach `max(sp)` with no full-kilometre splits.
Separately, a two-point track (0 m, 0 s) → (100 m, 60 s) makes `split_parts()` divide by
zero because its split point is the final point. Guard split availability and positive
segment distance/time; unavailable metrics should not prevent unrelated output.

## 11. concern — Empty GPX files crash instead of being skipped
`scripts/story.py:196-202`; `scripts/fetch.py:225-227`

The fetcher accepts `<gpx/>`, but `analyse()` unconditionally indexes the first and last
parsed points. Reproduced `IndexError` with that input; both story and sheet generation
consume this function. Reject unusable tracks while retaining their activity summaries.

## 12. concern — Multi-session FIT files mix incompatible totals and samples
`scripts/fit.py:77-91,97-112`

Session totals come from the *first* session, while speed records come from *every* session.
A two-session fixture produced 10 s elapsed, 25 s moving, and distance-curve timestamps
ending `[…, 20, 25, 10.0]`. Select records belonging to the chosen session, or decline the
device override when session association is ambiguous.

## 13. concern — An intervening sport prevents duplicate runs from merging
`scripts/analyze.py:90-95`

Deduplication compares only with the last chronological group. Reproduced: Run at T,
Walk at T+20 s, duplicate Run at T+51 s — all three remain, contrary to the
same-sport/180-second rule. Maintain candidate groups per sport.

## 14. concern — Duplicate backlinks can point to the wrong retained run
`scripts/sheet.py:84-85`; `scripts/story.py:877-878`

Consumers reconstruct membership using the first retained activity within 180 seconds
rather than the actual dedupe group. With newest-first runs at 190 s, 170 s, 0 s: dedupe
drops the 170 s run in favor of the 0 s run, but `duplicates_of()` links it to the 190 s
run. This also attaches comparison distances to the wrong session. Return the
dropped→kept mapping directly from deduplication.

## 15. concern — Reported kilometre splits are not measured at kilometre boundaries
`scripts/story.py:209-216`; `scripts/sheet.py:206-208`

Each split uses the first sample beyond its threshold, carrying sampling overshoot into
the reported duration. Constant 5:00/km movement sampled at (0 m, 0 s), (900, 270),
(1100, 330), (2000, 600) produced splits of 330 s and 270 s instead of 300 s each.
Interpolate exact crossings and handle multiple boundaries crossed between samples.

## 16. concern — Missing GPX understates totals and training load as though duplicates were removed
`scripts/story.py:854-855,867-891`

Summary totals and weekly load use only runs with GPX, while the comparison total includes
all outdoor runs. Reproduced four distinct 5 km runs with one missing GPX: the headline
reports 15 km, compares it with 20 km, and says zero duplicates were merged. Compute
summary metrics from deduplicated activities; restrict only track-dependent analysis to
usable GPX.

## 17. concern — Receiver ignores the configured spreadsheet ID
`scripts/sheet.py:299`; `sheets/webapp.gs:38-39`

The client sends `spreadsheetId`, but the receiver selects the active spreadsheet without
checking that ID. A configuration/deployment mismatch therefore writes to the container
rather than the requested destination. Open a server-configured spreadsheet explicitly
and reject requests whose ID does not match.

## 18. concern — Service errors are misclassified as expired authentication
`scripts/fetch.py:190-195,276-277`

HTTP 500 and non-JSON error pages become `SessionExpired`, causing the persisted expiry
marker and manual-login instructions. Reproduced HTTP 500 raising that exception. Reserve
session-expired handling for authentication evidence (redirect to `/login`, explicit 401)
and report upstream failures separately.

## 19. concern — Longer runs disappear from the distance chart
`scripts/progress.py:192-205`

The vertical scale is fixed at 12 km. A 16 km run rendered at `cy="-18.7"`, outside the SVG
viewport. Derive the upper bound from the plotted distances.

## 20. concern — Evening and overnight runs disappear from the start-hour chart
`scripts/story.py:552-562`

Counts include all hours, but rendering includes only 04:00–19:59. A history containing a
21:00 run produced zero activity bars. Render all 24 hours (or at least every populated
hour).

## 21. nit — Pace rounding produces invalid M:60 values
`scripts/analyze.py:39-41`; `scripts/progress.py:42-44`

Both formatters round the seconds remainder without carrying into minutes. Reproduced
359.6 s/km displaying as `5:60`. Round total seconds first, then `divmod(total, 60)`.

---

### Reproduction notes

All reproductions ran in the review session only: in-memory FIT fixtures built with the
`tests/test_fit.py` byte builders, `patch.object` on `Path.read_text`/`read_bytes`/
`write_text`, subprocess `bash -c` on extracted `sync.sh` lines, and a JS stub of the Apps
Script services around `sheets/webapp.gs`. No repo files were modified; `reports/story.html`
was rewritten by one probe and is gitignored. The full test suite passed before review
(`python3 -m unittest discover -s tests`).