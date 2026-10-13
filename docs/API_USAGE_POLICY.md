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

## NASA TechPort via api.nasa.gov — keyed, metered, cached

`https://api.nasa.gov/techport/api/...`, fronted by api.data.gov's API
Umbrella. Rules below read from the api.data.gov developer manual on
**2026-09-14**, not from memory.

**Never `techport.nasa.gov` directly** (AD-060). The direct host needs no
key and publishes no rate limit. An unmetered endpoint is not a generous
one — it is one where you find the ceiling by hitting it, and where
nothing tells you how close you are.

### The published rules, and what we do about each

| Rule as published | What enforces it here |
|---|---|
| **1,000 requests/hour** is the *default*; "rate limits may vary by service" | We no longer assert a number. `Client` reads `X-RateLimit-Limit` from every response and prints it. This key reports ~2,000 on TechPort, so the 1,000 in the original docstring was a guess that happened to be conservative |
| Counters reset **on a rolling basis**, not on the clock hour | We never reason about elapsed time. `X-RateLimit-Remaining` is read from every response and is the only quantity the stop rule consults |
| Exceeding the limit **temporarily blocks the key** | `QUOTA_FLOOR = 200`. The survey stops itself with a fifth of the budget unspent, so the block is a thing we stay clear of rather than recover from |
| `429 OVER_RATE_LIMIT` on exhaustion | Handled and named, though the floor should mean it is never seen |
| The key "should be kept private and should not be shared" | Passed in the **`X-Api-Key` header**, not the query string. A query parameter lands in server logs, proxy logs, `Referer`, browser history and — the one that bit us — inside `requests`' own exception messages. Never printed; `check_techport.py` reports its length and never its value |
| Errors may arrive as **JSON, XML, CSV or HTML** | The 403 branch keys on the documented error *code* appearing in the body, not on `Content-Type`. An earlier version assumed HTML meant "not api.data.gov" and was wrong in principle even though right in that instance |
| `API_KEY_UNVERIFIED` — registered but the confirmation email was never clicked | Listed in the 403 hint. It was missing from the first version |
| **DEMO_KEY: 30/hour AND 50/day, per IP** | `check_techport_403.py` is DEMO_KEY-only, spends 4, prints the remaining count and warns below 10. The daily cap is the one people forget |
| No caching rule is published | We cache anyway. `data/cache/techport/`, one file per project, 7-day TTL. See below |

### Caching, though nothing requires it

api.data.gov publishes a rate limit and no caching rule, so a re-run
would be *permitted* to refetch. We cache because "we were allowed to" is
not the standard this project works to — and because it has been on the
wrong side of that line before, re-downloading 13,517 `gp_history`
records for data already held. Three TechPort survey runs on 2026-09-14
refetched the same 50 details three times before the cache existed.

TTL is 7 days against records whose `lastUpdated` moves on the order of
months, so a shorter one would spend requests to observe nothing.

### Attribution

NASA-produced content is generally not subject to US copyright and asks
for **acknowledgement rather than permission** (AD-060). Anything derived
from TechPort that reaches the product surface must name NASA TechPort as
the source, in the same way GCAT's CC-BY credit is a condition and not a
courtesy. **This is an open item:** no TechPort-derived data has reached
a page yet, and the credit has to exist before any does.

### Standing usage

Survey-only. **Nothing is imported.** The 2026-09-14 surveys found a 3%
strict organisation match against `organizations` and no importer was
written, so current standing usage is ~51 requests per survey run,
falling to near zero on a re-run once the cache is warm.

**Status: low risk**, and the exposure is bounded by a floor that stops
the run rather than by an intention to be careful.

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

---

## USAspending.gov — added 2026-10-12

**Terms read 2026-10-05.** No authorization of any kind; the docs state
plainly that endpoints do not currently require it. Data is public under
the DATA Act, and the API repository is CC0-1.0.

### There is no published rate limit, and that is the hazard

Not in the API documentation, not in the
`fedspendingtransparency/usaspending-api` README, nowhere. A third-party
guide reports running 20 concurrent workers without a 429; that is
**evidence about one author's afternoon, not a term of service**, and it
is not a basis for anything.

AD-060 governs: **an unpublished ceiling is found by hitting it.** So the
ceiling is ours.

| Control | Value | Where |
|---|---|---|
| Minimum interval between requests | 1.0s | `MIN_INTERVAL_S` |
| Concurrency | 1 — never parallel | `Client.post` |
| Hard cap per run | 40 requests | `--max-requests` |
| Disk cache TTL | 7 days | `CACHE_TTL_S` |

The cap is not a performance setting. It is there so a bug cannot spend
a thousand calls while nobody is watching — the failure mode that took
eight days to notice in the TechPort job (AD-099).

### Pagination, and why a backfill must not use it

`POST /api/v2/search/spending_by_award/` caps at 100 rows per page,
about 10,000 records total, and degrades past offset 5,000. A real
backfill uses `POST /api/v2/download/awards/` (up to 500,000 records per
job) and is a separate piece of work. `--pages` on the ingester is for
sampling, not for loading history.

### Scope

NAICS 336414 / 336415 / 336419, contract award types A-D. Grants and
loans have different money semantics and are out of scope for the first
pass rather than silently mixed in.

---

## SEC EDGAR — added 2026-10-12, read at the source

**Two documents, and only one of them has the number.**

The EDGAR APIs page (`/search-filings/edgar-application-programming-interfaces`)
says the APIs "do not require any authentication or API keys to access"
and **states no rate limit at all**. The number is on the webmaster FAQ:

> "Note that our current maximum access rate is 10 requests per second."

An earlier draft of `src/funding/sec_edgar.py` credited the APIs page for
that figure. Correct number, wrong source — the same error as quoting a
rate limit for USAspending that nobody had published, run in reverse.
**Cite the document that says it.**

### The User-Agent is our policy, not EDGAR's rule

The FAQ says *"Please declare your user agent in request headers"* and
shows `Sample Company Name AdminContact@<domain>.com`. It does **not**
say the contact address is required, and it does not say what happens
when the limit is exceeded. It does mention an "Undeclared Automated
Tool" error, so an undeclared client can be refused.

`src/useragent.py` always declares project, version and purpose, so this
client is never undeclared. Requiring `CONTACT_EMAIL` on top of that is
**this project's choice** — an operator who can email you does not have
to block you — and the module says so rather than claiming a mandate.

| Control | Value |
|---|---|
| Published ceiling | 10 req/s (webmaster FAQ) |
| What we hold to | **5 req/s** — a ceiling is not a target |
| Hard cap per run | 60 requests (`--max-requests`) |
| Cache TTL | 7 days |
| Refuses to start | without `CONTACT_EMAIL` |

### Which endpoint, and why

| Endpoint | Used for |
|---|---|
| `submissions/CIK….json` | identity and SIC, in `--survey` only |
| `companyconcept/CIK…/us-gaap/<tag>.json` | **the claims.** One concept, one filer, small payload |
| `frames/us-gaap/<tag>/USD/CY….json` | **discovery and cross-checking only** |
| `companyfacts/CIK….json` | not used. Every concept a filer ever reported, when we want two |
| `companyfacts.zip` / `submission.zip` | recompiled nightly, whole dataset. **The answer for a real backfill**, not yet built |

**Frames never write a claim.** The docs say a frame takes the fact
"last filed that most closely fits the calendrical period requested",
with ±30 days tolerance on an annual period. That hands the choice of
vintage to a fitting rule we do not control, and `as_of_date` is part of
`cost_claims`' natural key — three hours after AD-107, where a vintage
error nearly stamped a 2026 total with a 1993 date. Period assignment
stays ours, stated in `annual_facts()`.

### SIC codes come from the SEC's own list, and ours were wrong

EDGAR assigns every filer a code from the Division of Corporation
Finance's SIC list. Read at the source 2026-10-12, the codes that put a
filer in scope for this project are:

| Code | SEC's own description |
|---|---|
| **3760** | Guided missiles & space vehicles & parts |
| 3720 / 3721 / 3724 / 3728 | Aircraft; aircraft; aircraft engines & parts; aircraft parts & auxiliary equipment NEC |
| 3812 | Search, detection, navigation, guidance, aeronautical systems |
| 3663 | Radio & TV broadcasting & communications equipment |
| 4812 | Radiotelephone communications |
| 4899 | Communications services, NEC |

`SPACE_SIC` shipped as `{"3760", "3761", "3663", "3669", "3812"}`, and
the Data Source Register has said **"SIC 3761/3663"** since 2026-10-05.
**There is no 3761 and no 3669 in the SEC's list.** Nothing filters on
the constant yet, so the error was invisible; the first query that used
it would have returned zero rows and read as "no US-listed space
companies file with the SEC" rather than as a typo. Same shape as
AD-105 — a tool reporting its own mistake as a finding about the world.
`tests/test_api_compliance.py` now asserts 3760 present and 3761/3669
absent, so the constant cannot drift back.

The codes are **reported, never used to filter** in `sec_edgar.py`: a
filer is in the seed because a person put it there with a written basis.
The SIC only ever adds a line to a report. They matter because the bulk
path below *is* organised by SIC.

### DERA Financial Statement Data Sets — the bulk discovery path

Separate from the APIs, the SEC's Division of Economic and Risk Analysis
publishes quarterly ZIPs of the numeric data from the face financials of
every XBRL filing: 2009 Q1 through 2026 Q3, ~62 MB each, flattened, and
— the reason they matter here — carrying **each filer's SIC code**. One
HTTP request per quarter, no per-filer walk, no rate limit to respect.

That makes them the honest answer to "who are all the space filers":
scan one quarter's ZIP for the SIC codes above and the universe falls
out, instead of 16 hand-curated tickers. It is the same conclusion
reached for USAspending — per-entity endpoints are for sampling, bulk is
for loading history.

**The SEC's own caveats, which must travel with any claim built from
them:**

- *"we cannot guarantee the accuracy of the data sets"*
- errors *"were introduced into the data sets during the process of
  extracting the data"*
- *"the data sets do not reflect all available information"* and
  *"they are not a substitute for such filings"*

So a DERA-derived number is **not** the same evidence as a
`companyconcept` number, even when both trace to one filing. If these
are ever ingested, `cost_claims.source` must distinguish
`sec_dera` from `sec_edgar` and the extraction caveat belongs in
`derivation_basis`, because the resolver's whole job is to keep two
claims about one fact apart rather than average them. **Not built.**
Recorded so the next person does not re-derive it.

### What is still required before this module makes a single request

1. `CONTACT_EMAIL` set in `.env` — the client exits without it.
2. Every `sec_ciks.tsv` row moved off `decision=pending`; `--apply`
   refuses the file otherwise.
3. Each `match` row's `org_code` present in `organizations`.

Nothing above is an EDGAR requirement. All three are this project's.

---

## Feeds — added 2026-10-13

`src/feeds/watch.py` watches RSS and Atom feeds so this project finds out
that something is new without re-downloading anything to discover it.
`data/seed/feeds.tsv` is the registry: one row per feed, with a written
basis, and a `verified` column that starts at **no**.

### Conditional GET is the whole politeness argument

A feed is the cheapest thing a publisher serves, and only if the client
sends back what it was given. This module stores each feed's `ETag` and
`Last-Modified` and sends `If-None-Match` and `If-Modified-Since` on
every poll, so an unchanged feed answers **304 with no body at all**.
The steady state of watching ten feeds is ten requests that transfer
nothing.

A watcher without conditional GET re-downloads every feed on every run
and is indistinguishable, from the operator's side, from a scraper.

| Control | Value |
|---|---|
| Requests per feed per run | **1** |
| Minimum interval per host | 2 s, raised by any published `Crawl-delay` |
| Hard cap per run | 40 requests, robots.txt fetches included |
| Measured cost of one full `--verify` | **12 requests** across 3 hosts |
| Unverified rows | **not polled** unless `--include-unverified` |
| Writes to the database | **none** |

### robots.txt: three outcomes, not two

RFC 9309 §2.3.1 distinguishes cases this module got wrong on the first
pass:

| robots.txt answers | What it means | What we do |
|---|---|---|
| 2xx | rules exist | parse and obey, `Crawl-delay` included |
| 4xx | *"crawlers MAY access any resources"* | fetch |
| 5xx or network error | *"crawlers SHOULD assume complete disallow"* | **skip the host** |

The first version collapsed 4xx and unreachable into one permissive
branch and its own docstring claimed the RFC said so. It does not. The
bug surfaced within the hour: `--verify` run from an environment whose
egress proxy answers 403 for every host printed *"treating as no
rules"* for a host it had never spoken to. **A host whose rules we
cannot read is not a host with no rules** — and guessing permissive
there is exactly what earns the IP block the standing instruction says
to avoid. There is deliberately no `--ignore-robots` flag.

This matters immediately for EDGAR. The SEC's own RSS page recommends
its *Latest Filings* Atom feed at
`/cgi-bin/browse-edgar?action=getcurrent&output=atom`, and the session's
fetcher refused that URL citing the target's robots rules. So a feed the
publisher recommends may sit on a path the publisher disallows. This
module resolves that by asking `robots.txt` at poll time rather than
taking anyone's word for it.

### A feed that is late is a finding, not silence

A watcher that reports only new items cannot tell *"nothing happened"*
from *"this feed died six months ago"* — both render as an empty list.
So `staleness()` reports **LATE** when a feed has been silent longer
than its own `stale_after_hours`, and **UNDATED** when it carries no
build date at all, which is deliberately not the same as ok.

**Corrected 2026-10-13, same day.** The first version of this section
reported all three SEC XBRL feeds as months stale, with a table of
`lastBuildDate` values in April and February 2026. **That was wrong.**
Those readings came from the session's fetcher, which caches, and the
section said so and called for re-measurement — which is the only
reason this is a correction and not a false entry in the register. Run
from the conda env at 03:34 UTC on 10-13, both feeds reported
`lastBuildDate` of **2026-10-12 21:50:03 ET**, minutes old at the time
and exactly where they should be. The feeds are healthy.

### The threshold is ours, and the first version stole it from a sentence

The same run produced a **false LATE** on both XBRL feeds, and the
cause was in this module, not at the SEC.

The first version derived its threshold from the publisher's claimed
cadence × 6. The SEC's sentence is *"updated every ten minutes **Monday
through Friday, 6am – 10pm EST**"*. The code kept the ten minutes and
dropped the conditions. At **23:34 ET on Monday** it found a feed last
built at **21:50 ET** — the last ten-minute slot before the 10pm
close — and called it dead. Nothing about the feed was wrong; it had
shut down for the night on schedule.

Two parts to the fix:

1. **The registry now carries `cadence_claimed` and
   `stale_after_hours` as separate columns.** The first is what the
   publisher says, documentation only, read by nothing. The second is
   this project's threshold, the only number the check uses. They can
   no longer be the same number by accident, and a threshold has a
   visible author instead of a multiplier nobody published.
2. **A `window` column.** `Mon-Fri 06:00-22:00 America/New_York`. Time
   outside the window does not count as silence, so Friday 21:55 to
   Monday 07:00 is 57 wall-clock hours and **one** in-window hour.

**Holidays are not modelled.** The window knows about nights and
weekends, not about Columbus Day. The SEC thresholds are therefore set
to 20h so that one closed weekday (16 in-window hours) cannot raise an
alarm alone and two consecutive ones can — a deliberate trade of one
missed alarm over a long federal weekend against a false alarm every
holiday, which is the kind of noise that gets a check ignored.

**Item age is not checked at all.** Both XBRL feeds' newest item on
10-13 was Friday 10-09, because 10-10 and 10-11 were the weekend and
Monday 10-12 was Columbus Day, a federal holiday on which EDGAR accepts
no filings. A feed rebuilt on time and correctly containing nothing is
healthy. Only `lastBuildDate` is read.

### What the first real run did find

| Feed | Result |
|---|---|
| 8 of 9 | HTTP 200, parsed, **conditional-GET headers offered by every one** |
| `sec_press`, `sec_litigation` | **no `lastBuildDate` element at all** → permanently UNDATED, uncheckable |
| `sec_xbrl_usgaap`, `sec_xbrl_all` | healthy; built 21:50 ET, 200 items each, newest item Friday (holiday weekend) |
| `nasa_technology` | 10 days between builds — normal for a topical feed, and why its threshold is 30 days not a week |
| `fed_register_sec` | **HTTP 200, 10,595 bytes of `text/html`** — an access gate, see below |

That last row is what `--verify` exists for: **a 200 is not a feed.**
And `--raw` then said exactly what it was, in one line:

```
HTTP 200  Content-Type: 'text/html; charset=utf-8'
<title>Federal Register :: Request Access</title>
```

A CSRF token, a CSP nonce, and a page whose own title states what the
site wants: **automated clients should request access rather than
fetch.** Not a broken URL and not a changed pattern — a closed door,
served with a 200 so that a careless client never notices.

**So the row is refused and commented out of the registry**, reason
attached, where neither `--verify` nor `--poll` can knock again. The
standing instruction is to violate no site's policy, and the response
to a gate is to stop requesting, not to look more like a browser.
`check_techport_403.py` declined to ship browser impersonation in
September for the same reason, and that precedent decides this.

Their sanctioned route for programs is the **Federal Register JSON API**
at `api.federalregister.gov`. It is not RSS and does not belong in
`feeds.tsv`; it belongs in this document once somebody reads its terms
from the publisher's own developer page. Unread, so unwritten. The same
applies to the FCC and NOAA agency ids, and to CelesTrak, ESA and arXiv
— `--check <url>` tests a candidate and prints what a registry row for
it would need, without writing anything, so adding a source honestly
costs one command.

### Nothing here writes to the database, on purpose

A feed item is evidence that a claim might exist. It is not a claim.
The architectural steer says money information will come from many
sources including news and be built up by inference over time — which
is an argument for a person, or a module that can state its reasoning,
writing `cost_claims` with a `derivation_basis` naming the item. It is
not an argument for a headline becoming a number with no vintage and no
author. `tests/test_feeds.py` asserts that `watch.py`'s executable code
contains no reference to `src.db`, `sqlalchemy`, `cost_claims` or
`DATABASE_URL`, because a docstring saying so is not a control (AD-098).

### What is required before the first poll

1. ~~`--verify --apply` from the pipeline's network.~~ **Done
   2026-10-13**: 8 of 9 verified. Only the cloud fetcher and the device
   VM cannot reach sec.gov and nasa.gov; the conda env can.
2. ~~`--raw fed_register_sec`~~ **Done**: an access gate. Refused.
3. The first `--poll` records a baseline and reports **no** new items by
   design. Run on 10-13; the next poll is the first real one.

### Modes

| Mode | Requests | Writes |
|---|---|---|
| `--list` | none | none |
| `--check <url>` | 1 (+robots) | none — for growing the registry |
| `--raw <key>` | 1 (+robots) | none — for diagnosing a fault |
| `--verify [--apply]` | 1 per feed | `verified`/`verified_on` only, with `--apply` |
| `--poll [--dry-run]` | 1 per feed, usually 304 | the seen-id state only |

### Growing the registry — 2026-10-14

Four candidates read from their publishers' own pages. **One was added.**

**arXiv — added, and it changed a constant.** Feed base read at
`info.arxiv.org/help/rss.html`: `https://rss.arxiv.org/rss/<category>`,
RSS 2.0 or Atom, *"Feeds are updated daily at midnight Eastern Standard
Time"*, several categories joinable with `+`. One row,
`astro-ph.IM+physics.space-ph`, so it costs one request — upstream of
the TechPort TRL data, since a sensor or propulsion method appears
there years before it has a TRL.

That page says *"Please review the Terms of Use for arXiv APIs before
using the arXiv RSS feeds"*, and the terms say:

> make no more than one request every three seconds, and limit requests
> to a single connection at a time

applying to *"all of the machines under your control as a whole"*, with
*"Attempt to circumvent rate limits"* listed as prohibited.

`MIN_INTERVAL_S` was **2.0**. That is a violation on the second arXiv
request of any run. It is now **3.0** — one global floor at the
strictest rule any source in the registry publishes, because a
per-host exception table is a thing somebody forgets to update
(AD-109), and the cost is about fifteen seconds a run. Nothing the SEC
or NASA publishes asks for more. Worth remembering that "all machines
under your control" means this floor is not the whole picture if
anything else here polls arXiv.

Its `stale_after_hours` is **72** with **no window**: arXiv does not
announce every night, this project has not read which nights, and
encoding a schedule we have not read is exactly AD-115.

**Three checked and not added**, recorded in `feeds.tsv` so nobody
looks twice — *"I could not find a feed"* and *"there is no feed"* are
different findings:

| Source | Finding |
|---|---|
| **CelesTrak** | **No feed exists.** The site links none and says *"Follow @TSKelso and @CelesTrak on Bluesky"*. It also says *"Please be sure to adhere to our usage policy when retrieving any CelesTrak data"*, and this project already reads CelesTrak elsewhere — so the answer is not to synthesise a feed from a social account |
| **FCC** | Daily Digest is **email only** — *"You can have the Digest emailed to you daily"* — and `www.fcc.gov/rss` returned a client error. The Digest is the publisher's route and a person can subscribe; a mailbox is not this module's business |
| **ESA** | Feeds exist, but `esa.int/Services/RSS_Feeds` says the list *"is available on the right"* and the sidebar did not render to the fetcher. The URLs were **not read**. Open it in a browser, then `--check` |

Nothing above went in as a guess. That is the whole rule.

### What the first real poll taught — 2026-10-14

The first `--poll` that could report anything reported 301 new items,
and two things in that output were defects here rather than findings
about the sources.

**`0 item(s)` followed by `ok`.** The arXiv feed came back HTTP 200,
correctly titled, freshly built, conditional-GET headers offered, and
**empty** — and the watcher called it healthy, because its only health
check read `lastBuildDate`. A feed can be rebuilt punctually and match
nothing, and no date will ever say so. The module written because *"an
absent result is the most confident falsehood a system can produce"*
produced one.

`emptiness()` now reports it, and the verdict follows a measurement
rather than a rule, because zero is not automatically wrong:

| State | Verdict |
|---|---|
| items present | ok |
| 0 items, never seen holding any | **FIRST-EMPTY** — nothing to compare against yet; poll again after its next build before believing the URL |
| 0 items, has held up to N before | **EMPTY** — either between cycles or the query stopped matching, and nothing here can tell those apart |

That last sentence names both explanations and declines to pick one,
because the module cannot distinguish them. **The arXiv row stays a
candidate until `--check https://rss.arxiv.org/rss/astro-ph.IM`
settles whether the `+` multi-category syntax works the way its help
page describes.** One request answers it.

**`200 new of 200 items`.** `sec_xbrl_all` turned over its entire
window in one day; `sec_xbrl_usgaap` turned over 93 of 200 in the same
period. The SEC accepts thousands of filings a day into a 200-item
feed, so at a daily poll everything between polls is gone.

**A fixed window on a firehose is a sampling instrument, not a change
log**, and "200 new" invites exactly the wrong reading — it looks like
a day's account and is a sample of the last few minutes. A full
turnover is now reported as such, beside the number. For the money
layer this is the conclusion already reached for USAspending and the
EDGAR bulk zips: **completeness comes from bulk; the feed answers
"something is happening".**

**And the digest now splits on SIC.** Of those 301 items about six
concerned space, and `sec_xbrl_all`'s first twelve were eight Morgan
Stanley 424B2 prospectuses while `Ondas Inc. [8-K SIC 3663]` — the one
that mattered — sat further down. The split uses the SEC's own list,
which is what it was read from source for, and the out-of-scope items
are **still counted out loud with their top codes**, so the filter
cannot hide its own effect. That is the difference between a digest and
a silent drop.

Sharing that list moved `SPACE_SIC` into a dependency-free
**`src/sic.py`**. `sec_edgar.py` imports `src.db.writer` and
`sqlalchemy` at module level, so importing it for the codes would have
dragged a database engine factory into the module whose contract is
that it does not touch the database — and a second copy of the dict is
AD-112 with the serial numbers filed off. A test asserts exactly one
definition exists.

### The arXiv row is unproven, and that is where it stays — 2026-10-14

`--check` on the single category settled the first question and opened
a better one.

| Measurement | Value |
|---|---|
| `rss.arxiv.org/rss/astro-ph.IM` | HTTP 200, `application/rss+xml`, **916 bytes** |
| Title | `astro-ph.IM updates on arXiv.org` — correct |
| Items | **0** |
| `lastBuildDate` | `2026-10-13T04:00:05Z`, read on 10-14 |
| Conditional GET | ETag offered |
| arXiv's own status page | `rss.arxiv.org/feed/status` → **"Status: GOOD"** |

So it is **not** the `+` multi-category syntax — a single category
behaves identically. The publisher believes its feed service is
healthy. The feed is reachable, well-formed, correctly titled, and
yields nothing.

**The cause is undetermined and is not being guessed.** One check
discriminates between what is left: a high-volume archive. Items there
means the emptiness is category-specific; zero there means the service
or our access to it. Until that is run the row keeps
`verified=no` — with all of the above written into its basis, so the
next person starts from the measurements rather than repeating them.

And the attempt to leave it `verified=yes` exposed a second defect in
the same hour. `emptiness()` printed **EMPTY** and `cmd_verify`
promoted the row anyway: the fault reported, and the surrounding code
behaving as though it had not been. That is AD-109's shape — a check
whose finding nothing acts on — and it would have let
`--verify --apply` flip the row back on every run. **`verified` has to
mean a working source, not a reachable URL**, so a feed yielding no
items is now never promoted, and a test pins the branch ordering.

### The digest's third case

The second poll was the first healthy one: **6 of 9 feeds answered 304
with no body**, which is the conditional-GET design doing exactly what
it was built for, and 15 new items instead of 301.

It also showed the SIC split was a case short. Six new `usgaap` items,
all carrying SICs, none in scope — pharma, finance, oil and gas — and
the digest printed all six, which implies they were judged relevant.
Three cases, not two:

| Items | Digest |
|---|---|
| No SIC at all (every NASA item) | print them; never filter |
| Coded, some in scope | print those, then count the rest with their top codes |
| Coded, none in scope | **"0 of 6 in scope"** and the top codes — not six arbitrary filings |
