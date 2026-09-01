# External API usage policy

This platform pulls from four external sources, three of which enforce
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
