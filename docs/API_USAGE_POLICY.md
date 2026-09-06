# External API usage policy

This platform pulls from six external sources, three of which enforce
usage limits and one of which has already suspended an account belonging
to this project. This document records what each allows, what we actually
do, and where the enforcement lives.

**Rule of thumb: never request data we already hold.** Every limit below
exists because the provider is giving away something expensive.

---

## Space-Track — strictest, previously suspended

**Limits (documented):** 30 requests/minute · 300 requests/hour ·
`gp_history` once per object per lifetime · SATCAT once/day.

**Enforcement — the most mature in the codebase:**

| Component | Role |
|---|---|
| `src/tracking/api_request_log.py` | Persistent request log, survives across runs |
| `src/tracking/tle_history_cache.py` | Never re-requests a satellite's history |
| `src/tracking/satcat_cache.py` | Honours the once-per-day SATCAT rule |
| `src/tracking/spacetrack_policy_check.py` | Pre-flight check; **refuses to run** if the plan looks non-compliant |
| `satellite_utils.SpaceTrackRateLimiter` | Enforces rate at the moment of each call |

The pre-flight check runs before any network call, including login, and
computes the request count from local caches alone. Do not weaken this.

### One ledger, one folder (consolidated 2026-09-05)

The mechanisms above only work if every caller reads the same ledger.
Until 2026-09-05 they did not. The Satellite Visibility Tool anchored its
caches to `BASE_DIR/data/*.db`; this repository anchored the same caches
to `TLE_DATA_DIR/*.sqlite3`. One account, two memories, neither aware of
the other — and the tool's `config.py` had the two history caches crossed
(`TLE_HISTORY_CACHE_DB` named a file holding `tle_cache.py`'s schema,
`GP_HISTORY_CACHE_DB` named a file that did not exist), so
`seed_tle_history.py` could not even import there.

What the ledger shows for 2026-08-06: **7 SATCAT requests against a
1/day limit**, four of them inside two seconds. Either seven requests
went out where one was permitted, or the logging double-counts — and not
being able to tell which is the problem, because
`spacetrack_policy_check.py` reads this same log to decide whether the
next run is safe.

**The rule now:** every file holding Space-Track state lives in
`TLE_DATA_DIR`, and both `.env` files name the same folder.

| Constant | File |
|---|---|
| `API_REQUEST_LOG_DB` | `api_request_log.sqlite3` |
| `SPACETRACK_BUDGET_DB` | `spacetrack_budget.sqlite3` |
| `TLE_HISTORY_CACHE_DB` | `tle_history_cache.sqlite3` — `tle_elements` + `coverage`, 49.8 M rows over 61,882 objects |
| `GP_HISTORY_CACHE_DB` | `gp_history_cache.sqlite3` — `tle_history` BLOBs, the 1/object/lifetime guard, 15,838 objects spent |
| `SATCAT_CACHE_DB` | `satcat_cache.sqlite3` |
| `CATALOG_CACHE_DB` | `catalog_cache.sqlite3` |
| `SATELLITE_CONFIDENCE_DB` | `satellite_confidence.sqlite3` |

`TLE_DATA_DIR` is `D:\Projects\Data\TLEs` — deliberately **not** the
OneDrive copy. A live SQLite file in a synced folder is how a conflicted
copy appears, and a conflicted copy is two ledgers again.

**Enforced by:** `tests/test_config_paths.py` fails if any of those
constants stops being built from `TLE_DATA_DIR`, and `check_ledger.py`
reports per-day usage against the documented limits and names any stray
ledger or cache left outside the folder. Run `check_ledger.py` before
anything that touches Space-Track.

### Incident 2026-09-05 — 13,517 gp_history re-downloads

**What the rules say** (space-track.org, re-read 2026-09-05):

| Class | Documented frequency |
|---|---|
| **GP_History** | **1 / lifetime.** "Historical data should be stored locally, not re-downloaded" |
| GP (current TLEs) | 1 / hour |
| SATCAT | 1 / day after 1700 UTC |
| Overall | ≤30 requests/minute, ≤300 requests/hour |

Plus: do not send per-satellite queries; combine objects into one
comma-delimited request.

**What happened.** A Satellite Visibility Tool run fetched 13,517
gp_history responses in eight minutes. **13,511 were for objects already
held locally** since 2026-08-22.

**Cause.** `TLEHistoryCache` was constructed with
`max_age_hours=TLE_HISTORY_CACHE_MAX_AGE_HOURS` — 24 hours — so any two
runs more than a day apart re-requested the whole catalogue. The
constant was shared with a cache that genuinely does expire. The code's
own comment said "orbital history is immutable once past" directly above
the line that expired it daily.

**Why nobody noticed.** The tool never called
`api_request_log.log_request`, so none of it reached the ledger — and
`spacetrack_policy_check.py` reads that ledger to decide whether the
next run is safe. Whether a rate limit was breached is still unknown:
13,517 objects over 468 seconds is ~68 requests at batch 200, or ~540 at
batch 25, which would exceed both ceilings. Only the ledger could tell
them apart, and it was not written.

**The distinction that resolves it.** Prediction accuracy needs
**current** elements — class GP, hourly, from the CelesTrak catalogue
refreshed every run. Confidence scoring needs **past** elements — class
GP_History, immutable, once per lifetime. Different data, different
policy class. Caching history permanently costs prediction accuracy
nothing, because prediction never reads it.

**Fixed:**

- `GP_HISTORY_CACHE_MAX_AGE_HOURS` added — effectively permanent — so
  history is stored locally as the policy asks, instead of reusing the
  24-hour constant.
- `historical_accuracy.py` now passes `GP_HISTORY_CACHE_DB` (imported and
  unused until now) rather than `TLE_HISTORY_CACHE_DB`, so BLOB responses
  stop being written into the 6.7 GB elements archive.
- The login and the rate limiter's `log_callback` now write to
  `api_request_log`, so the pre-flight check can see tool traffic.
- The two response sets were merged: 15,844 objects in
  `gp_history_cache.sqlite3`, preferring the newer payload where both
  existed. Verified: a run today, tomorrow or in a year re-requests **0**.

**Rule derived:** a cache expiry is a policy decision, not a performance
tuning knob. When the provider says "store locally, do not re-download,"
an expiry silently converts that into repeated downloads — and sharing
one constant between two caches with different rules is how it happens.

---

**Status: compliant.** Leave it alone unless the limits change.

---

## CelesTrak — no account, but blocks abusers

**Policy:** GP data regenerates roughly every 2 hours. Requesting a group
again inside that window returns a plain-text notice —
`GP data has not updated since your last successful ...` — and persistent
over-requesting gets the client blocked. There is no account to suspend,
which makes a block harder to appeal, not easier.

**Note:** CelesTrak answers an unknown group with **HTTP 200** and an
`Invalid query` body. Status codes cannot detect a bad group name; only
parsing can.

**What we do (as of 2026-09-01):**

- 5 groups per run, not 17. `active` returned 16,463 objects while the
  twelve other configured groups downloaded a further 12,825 to contribute
  **131** the catalogue did not already have. Only groups holding objects
  `active` excludes are fetched: `analyst` and three debris events.
- **60 requests/day** at the 2-hourly cron, down from 204.
- 3 seconds between requests.
- `SATELLITE_DB_DIR/celestrak_fetch_log.json` records the last successful
  fetch per group and skips anything inside the 2-hour window. `--force`
  overrides.
- `--probe-groups` (~35 requests) requires `--force` and explains why.

**Why the guard exists:** on 2026-09-01, 80 requests went out from one IP
while debugging group names, 65 of them within ten minutes. Nothing in the
code prevented it. Now something does.

**Adding a group** must be justified by objects it uniquely contains.
`--check-groups` verifies the configured list still resolves; run it after
editing, and consider it a CI candidate.

---

## N2YO — generous, used lightly

**Limits:** 1,000 transactions/hour **per endpoint category** (free tier).
Only `/positions` is used, so that budget is not shared.

**What we do:** `src/tracking/compare_n2yo.py` is a manual methodology
spot-check — it validates our azimuth/elevation against theirs. It is not
called by any workflow.

- 5 satellites per run = 5 requests. Hard cap of 25 (`MAX_SATELLITES_ALLOWED`).
- 1 second between requests.
- Every response carries `info.transactionscount`, our usage this hour as
  reported by N2YO. This is now read, displayed, and used to abort with 100
  transactions to spare rather than discovering the limit by hitting it.

**Status: low risk.** The only realistic failure is someone raising
`NUM_SATELLITES_TO_CHECK`; the cap makes that fail loudly.

---

## GCAT — CC-BY, monthly, cached

Jonathan McDowell's General Catalog of Artificial Space Objects,
`https://planet4589.org/space/gcat/tsv/derived/currentcat.tsv`.

**Licence:** CC-BY. Attribution is a condition of use, not a courtesy —
credit McDowell / GCAT wherever the data is displayed or redistributed.

**Cadence:** GCAT updates roughly monthly. A 24-hour cache is therefore
generous; `satellite_utils.fetch_gcat_catalog()` enforces it via
`GCAT_CACHE_MAX_AGE_HOURS`, and failure to fetch degrades to "continue
without GCAT" rather than crashing.

**Note:** the parser reads columns by hardcoded index and GCAT has
changed column order before. It carries an explicit drift check that
warns when NORAD IDs parse but owner/state come back blank for >90% of a
sample — the same silent-failure shape as the malformed `gp_history`
response. If Phase 2 extends the parser to more columns, extend that
check with it.

**Status: low risk.** One request per day at most, to a file that
changes monthly.

---

## UCS Satellite Database — manual download only

`https://www.ucs.org/resources/satellite-database`

**Status of the source itself:** paused. Data current through
**2023-05-01**; the page was last updated January 2024 and states that
UCS is evaluating whether to resume. Treat it as a fixed historical
snapshot, not a feed.

**No automated retrieval.** Download the Excel or tab-delimited file by
hand from the page and commit it to `data/seed/`. There is nothing to
poll — the file has not changed in over three years — so a fetcher would
be pure downside: it would add a scraping surface against a nonprofit's
web host in exchange for re-downloading a static file.

**Status: no exposure**, and it stays that way as long as acquisition
stays manual.

---

## Copernicus / NASA Earthdata / USGS — imagery

Used by `src/imagery/ingest.py`. Scene downloads are large and manual;
the pipeline archives locally to `D:\SatelliteData\raw` and never
re-downloads a scene it already holds. Not yet audited in the same detail
as the above — **do that before any imagery fetching is automated.**

---

## If a limit is ever hit

1. **Stop.** Do not retry, and do not switch to a different key or IP.
2. Check the request log — `ingestion_log`, `api_request_log`, or
   `celestrak_fetch_log.json` — and work out what actually went out.
3. Fix the cause before running again. Every incident so far has been a
   missing guard, not bad luck.
4. Record it here.
