# Phase 2 seed data

Small, versioned reference files that catalogue enrichment must be
reproducible against.

The root `.gitignore` blanket-ignores `*.csv` for scene data and exports;
this directory is explicitly un-ignored. Check that `git status` sees a
file here before assuming it is tracked.

---

## The state of the catalogue

`check_catalog.py` on 2026-09-04, immediately after
`004_catalog_provenance.sql`:

    satellites: 18,054 rows
    every descriptive column: 0
    enriched: 0

That is the correct baseline. CelesTrak's OMM feed supplies name,
catalogue number, international designator and orbital elements;
`fetcher.py` writes those and nothing else. All eighteen descriptive
columns are Phase 2's to fill.

---

## Sources, in the order they should be used

The Phase 2 sprint plan named UCS first. That was wrong, and the reason
is worth recording: **UCS is frozen and partial, and two better sources
were already half-built inside this repository.**

### 1. SATCAT — eleven columns, whole catalogue, exact join

CelesTrak's SATCAT (`https://celestrak.org/satcat/`), or Space-Track's,
which `src/tracking/satcat_cache.py` already fetches under the
once-per-day rule.

| SATCAT field | `satellites` column |
|---|---|
| `NORAD_CAT_ID` | join key |
| `OBJECT_TYPE` | `object_type` |
| `OPS_STATUS_CODE` | `status` |
| `OWNER` | `country_code` |
| `LAUNCH_DATE` | `launch_date` |
| `LAUNCH_SITE` | `launch_site` |
| `PERIOD` | `period_min` |
| `INCLINATION` | `inclination_deg` |
| `APOGEE` | `apogee_km` |
| `PERIGEE` | `perigee_km` |
| `RCS` | `rcs_size` |
| `ORBIT_TYPE` | `orbit_type` |

Eleven of eighteen columns, for essentially every row, joined on
`norad_id` — `match_method = 'norad_id'`, `source_confidence = 1.0`.
**No fuzzy matching, so no false-positive risk at all.** This is the
backbone; everything else is a supplement to it.

It also does not need a manual download or a new account, and the
CelesTrak rate-limit guard in `fetcher.py` already covers the host.

### 2. GCAT — CC-BY, whole catalogue, monthly

Jonathan McDowell's General Catalog, `https://planet4589.org/space/gcat/`.
`satellite_utils.fetch_gcat_catalog()` already downloads and caches
`tsv/derived/currentcat.tsv` with a 24-hour TTL **and a column-drift
warning** — GCAT has changed column order before, and the existing
parser detects it rather than silently returning blanks.

**A copy is already on disk**, 14 MB, fetched 2026-08-26:
`D:\Projects\Satellite Project\Satellite Visibility Tool\data\gcat_currentcat.tsv`.
No request is needed to start work against it. Its header is

    JCAT  DeepCat  Satcat  Piece  Active  Type  Name  LDate  Parent
    Owner  State  SDate  ExpandedStatus  DDate  ODate  Period
    Perigee  PF  Apogee  AF  Inc  IF  OpOrbit

which confirms the existing parser's column indices (2, 7, 9, 10, 12 =
Satcat, LDate, Owner, State, ExpandedStatus) are correct against the
current file.

Two corrections to an earlier assumption. `currentcat.tsv` carries **no
mass column** — GCAT keeps mass in a separate table, so mass has to come
from UCS or a second GCAT file. And it does carry `DeepCat`, `Parent`
and `DDate`, which are the fields the deep-space and decayed-object
scopes need.

Covers post-2023 objects, which is the gap UCS cannot fill.

### 3. UCS — narrow, stale, and still necessary

`https://www.ucs.org/resources/satellite-database`

**Read this before planning around it:** the database has *paused
updates*. Data is current only through **2023-05-01**, and the page was
last touched January 2024. UCS says it is "evaluating plans for resuming
the project."

Two consequences:

- It covers **active payloads only** — roughly 7,500 objects — not the
  18,054 in our catalogue. Debris and rocket bodies are absent by design.
- Everything launched since May 2023 is missing, which is a large share
  of the current catalogue and almost all recent constellation growth.

So UCS cannot be the backbone. What it uniquely has is `users`
(civil / commercial / government / military), `purpose`, `manufacturer`
and `expected_lifetime_yr` — and `users` and `purpose` are exactly what
Phase 3's industry work groups by. That is why it is still on the list,
and why `004` added the `users` column for it.

**Obtain it by hand.** Download the Excel or tab-delimited file from the
page above in a browser and drop it in this directory. Do not write a
scraper: see `docs/API_USAGE_POLICY.md`. Record the file's date in the
commit message — a frozen source makes provenance a dating problem.

Match on `intl_designator` where present (`match_method =
'intl_designator'`, confidence 0.9), falling back to name. Once SATCAT
has landed, a UCS name match can be **corroborated against SATCAT's
launch date** before it is accepted — a check that does not exist if UCS
goes first. That, on its own, is a reason for this order.

---

## Why the order matters more than it looks

Each step makes the next one safer:

1. SATCAT fills eleven columns with no matching risk, and gives every
   row a launch date.
2. GCAT is then checked *against* SATCAT rather than trusted alone.
3. UCS's fuzzy name matches — the only real false-positive risk in
   Phase 2 — are validated against a launch date already in the row.

Run in the planned order, UCS's fuzzy matches would have been accepted
on nothing but string similarity, into a schema whose own migration says
*"prefer leaving a satellite unattributed to attributing it wrongly."*

---

## Provenance discipline

Every enrichment pass sets `data_source`, `match_method`,
`source_confidence` and `matched_at` on the rows it writes. Run
`python check_catalog.py` after each pass: it exits non-zero if any row
gained a descriptive value without provenance.

Note that the provenance columns are row-level, not per-field — see the
design note in `004_catalog_provenance.sql`. Once these three sources
overlap they *will* disagree, and that is the point at which a
`satellite_attribution` table keyed `(norad_id, field, source)` earns
its keep.
