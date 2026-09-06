"""
Enrich the satellite catalogue from CelesTrak's SATCAT.

WHY THIS SOURCE, AND WHY FIRST
==============================
Phase 2's sprint plan named the UCS Satellite Database first. Checking the
source before writing the matcher showed that to be the wrong order:

  * UCS paused updates. Its data stops at 2023-05-01, and it covers
    active payloads only - roughly 7,500 objects against our 18,054.
    Everything launched since is absent, which is most recent
    constellation growth.

  * SATCAT covers the whole catalogue, is current, and joins on
    NORAD_CAT_ID - an exact key. No fuzzy matching, so none of the
    false-positive risk that 004's own comments warn about.

So SATCAT is the backbone and UCS is a supplement to it. Ordering matters
beyond coverage: once SATCAT has written a launch date for every object, a
later UCS name match can be corroborated against it. Run the other way
round, a UCS fuzzy match would be accepted on string similarity alone.

WHY THIS RUNS AS A SURVEY FIRST
===============================
Three SATCAT fields are coded vocabularies - OBJECT_TYPE, OPS_STATUS_CODE,
ORBIT_TYPE - and CelesTrak's format documentation names the fields without
enumerating their permitted values. Writing a mapping from memory and
defaulting the leftovers to 'UNKNOWN' would reproduce this project's most
expensive recurring bug: a configuration naming something the world does
not have, paired with a fallback that makes the absence look like success.
It has cost seven separate outages already.

So `--survey` fetches once, writes nothing, and reports what the file
actually contains: every distinct code, with counts, and every code this
module cannot map. Fix the maps against that output; only then apply.
Unmapped values are an error, never a silent 'UNKNOWN'.

THE TWO SCHEMA TRAPS
====================
1. `country_code` is `VARCHAR(3) REFERENCES countries(code)`, and
   `countries` is seeded with ~20 ISO 3166-1 alpha-3 codes. SATCAT's
   OWNER field is neither ISO nor country-only: it uses its own
   abbreviations (US, PRC, CIS, UK) and includes organisations that have
   no ISO code at all (ESA, EUME, ITSO, NATO, GLOB). Several are four
   characters and will not even fit the column.

   Writing OWNER into country_code would fail the foreign key on most
   rows. The fix is a separate `owner_code TEXT` holding SATCAT's value
   verbatim, with country_code resolved only where a confident ISO
   mapping exists - lossless, and it prefers unattributed to wrong.
   `--survey` reports how many owners resolve; the migration adding
   owner_code is a prerequisite for `--apply`.

2. `orbit_type` is NOT mappable from SATCAT's ORBIT_TYPE. The schema
   comment says "more specific: polar, sun-sync, etc." - a description of
   the orbit's geometry. SATCAT's ORBIT_TYPE describes the object's
   disposition instead (in orbit, landed, impacted, docked). Same name,
   different meaning. It is deliberately not in COLUMN_MAP; deriving
   orbit_type belongs with inclination, which we will have.

WHAT THIS FILLS
===============
Ten columns, for essentially every row, at confidence 1.0:

    object_type  status      launch_date  launch_site  period_min
    inclination_deg  apogee_km  perigee_km  rcs_size   owner_code

plus country_code where the owner resolves to a seeded ISO code.

REQUEST BUDGET
==============
One request per run, to a file CelesTrak regenerates daily. It shares
fetcher.py's `celestrak_fetch_log.json` under the key 'satcat' and skips
inside the window - the same courtesy guard added after 2026-09-01, when
80 requests went out from one IP in ten minutes. `--force` overrides.

    python -m src.catalog.seed_satcat --survey
    python -m src.catalog.seed_satcat --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
import uuid
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone

import requests

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

from src.tle.fetcher import (
    _load_fetch_log,
    _record_fetch,
    _seconds_since_fetch,
)

SATCAT_URL = "https://celestrak.org/pub/satcat.csv"
FETCH_LOG_KEY = "satcat"

#: CelesTrak regenerates SATCAT daily. Anything under this is a re-request
#: for data that cannot have changed.
MIN_FETCH_INTERVAL_S = 20 * 3600

REQUEST_TIMEOUT = 120
USER_AGENT = "satellite-platform/1.0 (catalogue enrichment; 1 req/day)"

# ══════════════════════════════════════════════════════════════════════════
#  The file's shape
# ══════════════════════════════════════════════════════════════════════════
#
# From CelesTrak's SATCAT format documentation. Asserted against the real
# header on every run: a column inserted upstream would otherwise shift
# every field silently, which is precisely how the GCAT parser in
# satellite_utils.py learned to check itself.

EXPECTED_HEADER = [
    "OBJECT_NAME", "OBJECT_ID", "NORAD_CAT_ID", "OBJECT_TYPE",
    "OPS_STATUS_CODE", "OWNER", "LAUNCH_DATE", "LAUNCH_SITE",
    "DECAY_DATE", "PERIOD", "INCLINATION", "APOGEE", "PERIGEE",
    "RCS", "DATA_STATUS_CODE", "ORBIT_CENTER", "ORBIT_TYPE",
]

#: SATCAT field -> satellites column, for values that need no translation.
COLUMN_MAP = {
    "LAUNCH_DATE": "launch_date",
    "LAUNCH_SITE": "launch_site",
    "PERIOD":      "period_min",
    "INCLINATION": "inclination_deg",
    "APOGEE":      "apogee_km",
    "PERIGEE":     "perigee_km",
}

# ══════════════════════════════════════════════════════════════════════════
#  Coded vocabularies
# ══════════════════════════════════════════════════════════════════════════
#
# Operational status codes are documented at celestrak.org/satcat/status.php
# and are the one vocabulary here that is published in full.
#
# Our `status` column is VARCHAR(20) with the vocabulary
# ACTIVE / INACTIVE / DECAYED / UNKNOWN, so several SATCAT codes collapse.
# CelesTrak defines "active" as +, P, B, S or X, and that definition is
# followed here rather than invented.

OPS_STATUS_MAP = {
    "+": "ACTIVE",      # Operational
    "P": "ACTIVE",      # Partially operational
    "B": "ACTIVE",      # Backup/standby
    "S": "ACTIVE",      # Spare
    "X": "ACTIVE",      # Extended mission
    "-": "INACTIVE",    # Nonoperational
    "D": "DECAYED",
    "?": "UNKNOWN",
    "":  None,          # absent, not unknown - leave the column NULL
}

#: Believed values, NOT documented by CelesTrak as an enumeration.
#: --survey prints anything absent from this map; add it there, do not
#: widen the fallback.
OBJECT_TYPE_MAP = {
    "PAY": "PAYLOAD",
    "R/B": "ROCKET BODY",
    "DEB": "DEBRIS",
    "UNK": "UNKNOWN",
    "":    None,
}

# ══════════════════════════════════════════════════════════════════════════
#  RCS: a number upstream, a bucket here
# ══════════════════════════════════════════════════════════════════════════
#
# SATCAT gives radar cross section in square metres. Our rcs_size is
# VARCHAR(10) documented SMALL / MEDIUM / LARGE, matching Space-Track's
# convention: SMALL < 0.1 m2, MEDIUM 0.1-1.0 m2, LARGE > 1.0 m2.
#
# Bucketing discards precision we were given. That is the schema's choice,
# not this module's, and it is recorded here so a later migration that
# wants rcs_m2 knows the number was available and thrown away.
#
# RCS IS A LEGACY FIELD. DO NOT GROUP BY IT WITHOUT A DATE FILTER.
# ===============================================================
# Measured against the cached SATCAT on 2026-09-05, on-orbit payloads by
# launch decade:
#
#     1960s   281 payloads   90.7% carry RCS
#     1970s   574                87.1%
#     1980s   755                90.6%
#     1990s   853                90.5%
#     2000s   677                91.1%
#     2010s 1,296                53.8%
#     2020s 15,569                0.0%
#
# Not one payload launched in the 2020s has an RCS value. Starlink
# (11,090 on-orbit payloads), OneWeb (654) and the Chinese
# megaconstellations (442) are all zero; everything else averages 45%.
#
# So `rcs_size` describes a shrinking, ageing minority of what is in
# orbit, and any analysis that groups by it is silently an analysis of
# pre-2020 objects. That is not a bug in this module and not something a
# different source fixes - the field simply stopped being published.
#
# It also explains a number worth not chasing twice: the catalogue came
# out at 9.6% rcs_size coverage against 17.6% for all on-orbit payloads.
# The catalogue skews to recent launches, and recent launches have none.

RCS_SMALL_MAX = 0.1
RCS_MEDIUM_MAX = 1.0


def rcs_bucket(value: str):
    if not value or not value.strip():
        return None
    try:
        m2 = float(value)
    except ValueError:
        return None
    if m2 < RCS_SMALL_MAX:
        return "SMALL"
    if m2 <= RCS_MEDIUM_MAX:
        return "MEDIUM"
    return "LARGE"


# ══════════════════════════════════════════════════════════════════════════
#  Owner codes
# ══════════════════════════════════════════════════════════════════════════
#
# SATCAT owner abbreviation -> ISO 3166-1 alpha-3, for the codes seeded in
# `countries` by 001_core_schema.sql. Deliberately incomplete: an owner
# absent here writes owner_code and leaves country_code NULL, which is the
# correct outcome for ESA, NATO, ITSO and the other non-state operators
# that a table called `countries` should not acquire rows for.
#
# CIS is left unmapped on purpose. It denotes the former Soviet Union, and
# collapsing it to RUS would assert a succession this project has no basis
# to assert for every object under it.

OWNER_TO_ISO = {
    "US": "USA",  "PRC": "CHN", "UK": "GBR",  "FR": "FRA",
    "GER": "DEU", "JPN": "JPN", "IND": "IND", "ISRA": "ISR",
    "CA": "CAN",  "AUS": "AUS", "BRAZ": "BRA", "IT": "ITA",
    "SPN": "ESP", "NETH": "NLD", "SKOR": "KOR", "UAE": "ARE",
    "NOR": "NOR", "SWED": "SWE", "UKR": "UKR", "RUS": "RUS",
}


def fetch_decision(age_s, cache_exists: bool, force: bool) -> str:
    """
    Fetch, or reuse what we have? Returns 'fetch', 'cache' or
    'fetch_uncached'.

    Split out as a pure function because the rule is easy to get subtly
    wrong, and the wrong version pushes people toward --force.

    The guard exists to stop us re-requesting data we ALREADY HOLD. So a
    missing cache is not a reason to refuse — it means the premise does
    not apply. The first version raised instead, and the only route
    forward was --force, which is precisely the habit that produced 80
    requests from one IP on 2026-09-01. A guard that can only be
    satisfied by overriding it has taught the wrong lesson.
    """
    if force:
        return "fetch"
    if age_s is None or age_s >= MIN_FETCH_INTERVAL_S:
        return "fetch"
    return "cache" if cache_exists else "fetch_uncached"


def _cache_path() -> Path:
    """Beside celestrak_fetch_log.json, so the guard and the data agree."""
    base = os.environ.get("SATELLITE_DB_DIR", r"D:\Databases\satellite")
    return Path(base) / "satcat_cache.csv"


def fetch_satcat(force: bool = False) -> str:
    """
    One GET per day, and the response is kept.

    KEEPING IT MATTERS MORE THAN IT LOOKS. --survey, --dry-run and
    --apply are three passes over the same daily file. Without a cache,
    the fetch-log guard correctly refuses the second and third, and the
    only way forward is --force — which teaches the habit of overriding a
    rate-limit guard as routine. That is how 80 requests went out from
    one IP on 2026-09-01.

    So the guard stays strict and the data gets reused: survey, inspect,
    then apply, on one download.
    """
    cache = _cache_path()
    log_data = _load_fetch_log()
    age = _seconds_since_fetch(FETCH_LOG_KEY, log_data)
    decision = fetch_decision(age, cache.exists(), force)

    if decision == "cache":
        print(f"Using the SATCAT fetched {age/3600:.1f} h ago ({cache}).")
        print("CelesTrak regenerates it daily; re-requesting would return "
              "the same file.")
        return cache.read_text(encoding="utf-8")

    if decision == "fetch_uncached":
        print(f"The fetch log says SATCAT was retrieved {age/3600:.1f} h "
              f"ago, but no copy was kept")
        print(f"at {cache}, so fetching once to fill it. A request for "
              f"data we do not hold is")
        print("not a redundant request, and this is the last time it "
              "should be needed.")

    resp = requests.get(SATCAT_URL, timeout=REQUEST_TIMEOUT,
                        headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()

    # CelesTrak answers some bad requests with HTTP 200 and a prose body -
    # the trap documented in docs/API_USAGE_POLICY.md. A status check
    # cannot see it; only looking at the content can.
    head = resp.text[:200].lstrip()
    if not head.startswith("OBJECT_NAME"):
        raise RuntimeError(
            "SATCAT response does not begin with the expected CSV header. "
            "First 200 characters:\n" + head)

    _record_fetch(FETCH_LOG_KEY)
    try:
        cache = _cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(resp.text, encoding="utf-8")
    except OSError as exc:
        # Not fatal — it only costs a re-request next pass — but say so,
        # because a silently absent cache is what makes --force look
        # necessary.
        print(f"  (could not cache SATCAT: {exc})")
    return resp.text


def parse(text: str) -> "list[dict]":
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    if header != EXPECTED_HEADER:
        raise RuntimeError(
            "SATCAT column layout has changed.\n"
            f"  expected: {EXPECTED_HEADER}\n"
            f"  received: {header}\n"
            "Every field below is read by name from this header, so a "
            "changed layout must be reviewed rather than absorbed.")
    return [dict(zip(header, row)) for row in reader if row]


def survey(rows: "list[dict]") -> int:
    """Report what the file contains. Writes nothing."""
    print(f"\nSATCAT: {len(rows):,} records\n")

    problems = 0
    for field, mapping, label in (
            ("OBJECT_TYPE", OBJECT_TYPE_MAP, "object_type"),
            ("OPS_STATUS_CODE", OPS_STATUS_MAP, "status"),
    ):
        counts = Counter(r[field].strip() for r in rows)
        print(f"{field} -> {label}")
        for code, n in counts.most_common():
            known = code in mapping
            target = mapping.get(code, "?")
            flag = "" if known else "   <-- NOT MAPPED"
            shown = repr(code) if code == "" else code
            print(f"  {shown:<8}{n:>9,}  {str(target):<14}{flag}")
            if not known:
                problems += 1
        print()

    # ORBIT_TYPE is surveyed but never written - see the module docstring.
    counts = Counter(r["ORBIT_TYPE"].strip() for r in rows)
    print("ORBIT_TYPE (surveyed only, deliberately not written)")
    for code, n in counts.most_common(10):
        print(f"  {repr(code) if code == '' else code:<8}{n:>9,}")
    print()

    owners = Counter(r["OWNER"].strip() for r in rows)
    resolved = sum(n for o, n in owners.items() if o in OWNER_TO_ISO)
    too_long = [o for o in owners if len(o) > 3]
    print(f"OWNER: {len(owners)} distinct codes")
    print(f"  resolve to a seeded ISO country: {resolved:,} rows "
          f"({resolved*100.0/max(len(rows),1):.1f}%)")
    print(f"  longer than country_code's VARCHAR(3): {len(too_long)} codes "
          f"({', '.join(sorted(too_long)[:12])}"
          f"{', ...' if len(too_long) > 12 else ''})")
    print("  top 15 unresolved:")
    for owner, n in owners.most_common():
        if owner not in OWNER_TO_ISO:
            print(f"    {owner:<8}{n:>9,}")
    print()

    rcs_present = sum(1 for r in rows if rcs_bucket(r["RCS"]))
    dates = sum(1 for r in rows if r["LAUNCH_DATE"].strip())
    print(f"RCS parses to a bucket for {rcs_present:,} rows "
          f"({rcs_present*100.0/max(len(rows),1):.1f}%)")
    print(f"LAUNCH_DATE present for {dates:,} rows "
          f"({dates*100.0/max(len(rows),1):.1f}%)")

    print()
    if problems:
        print(f"PROBLEMS: {problems} unmapped code(s). Add them to the maps "
              f"in this module before --apply.")
        print("Do NOT widen a fallback to absorb them.")
        return 1
    print("OK - every coded value in this file has an explicit mapping.")
    return 0


def to_row(rec: dict) -> "dict | None":
    """One SATCAT record -> the columns we are willing to write."""
    try:
        norad = int(rec["NORAD_CAT_ID"])
    except (ValueError, KeyError):
        return None

    obj_code = rec["OBJECT_TYPE"].strip()
    status_code = rec["OPS_STATUS_CODE"].strip()
    if obj_code not in OBJECT_TYPE_MAP:
        raise KeyError(f"unmapped OBJECT_TYPE {obj_code!r} (norad {norad})")
    if status_code not in OPS_STATUS_MAP:
        raise KeyError(f"unmapped OPS_STATUS_CODE {status_code!r} "
                       f"(norad {norad})")

    owner = rec["OWNER"].strip()
    row = {
        "norad_id":    norad,
        "object_type": OBJECT_TYPE_MAP[obj_code],
        "status":      OPS_STATUS_MAP[status_code],
        "owner_code":  owner or None,
        "country_code": OWNER_TO_ISO.get(owner),
        "rcs_size":    rcs_bucket(rec["RCS"]),
    }
    for field, col in COLUMN_MAP.items():
        raw = rec[field].strip()
        if not raw:
            row[col] = None
        elif col in ("period_min", "inclination_deg", "apogee_km",
                     "perigee_km"):
            try:
                row[col] = float(raw)
            except ValueError:
                row[col] = None
        else:
            row[col] = raw

    row["data_source"] = "celestrak_satcat"
    row["match_method"] = "norad_id"
    row["source_confidence"] = 1.0
    row["matched_at"] = datetime.now(timezone.utc)
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--survey", action="store_true",
                      help="Fetch once and report the file's vocabularies. "
                           "Writes nothing. Run this first.")
    mode.add_argument("--dry-run", action="store_true",
                      help="Build the rows and show a sample without writing")
    mode.add_argument("--apply", action="store_true",
                      help="Write attribution onto satellites that already "
                           "exist. Never creates rows - see the note in "
                           "writer.upsert_satellite_attribution.")
    ap.add_argument("--force", action="store_true",
                    help="Fetch even if the log says SATCAT is fresh")
    ap.add_argument("--limit", type=int, default=10,
                    help="Rows to show in --dry-run (default 10)")
    args = ap.parse_args(argv)

    rows = parse(fetch_satcat(force=args.force))

    if args.survey:
        return survey(rows)

    built, skipped, unmapped = [], 0, []
    for rec in rows:
        try:
            row = to_row(rec)
        except KeyError as exc:
            unmapped.append(str(exc))
            continue
        if row is None:
            skipped += 1
        else:
            built.append(row)

    if unmapped:
        print(f"\n{len(unmapped):,} records carry an unmapped code. "
              f"First five:")
        for msg in unmapped[:5]:
            print(f"  {msg}")
        print("\nRun --survey for the full vocabulary, then extend the maps.")
        return 1

    print(f"\nBuilt {len(built):,} rows ({skipped:,} without a usable "
          f"catalogue number).")
    resolved = sum(1 for r in built if r["country_code"])
    print(f"  country_code resolved: {resolved:,}")
    print(f"  owner_code present:    "
          f"{sum(1 for r in built if r['owner_code']):,}")
    print(f"\nFirst {args.limit}:")
    for r in built[:args.limit]:
        print(f"  {r['norad_id']:>7}  {str(r['object_type']):<12}"
              f"{str(r['status']):<10}{str(r['owner_code']):<6}"
              f"{str(r['country_code'] or '-'):<5}"
              f"{str(r['launch_date'] or '-'):<12}"
              f"{str(r['rcs_size'] or '-')}")

    if args.dry_run:
        print("\nNothing was written (--dry-run).")
        return 0

    # -- apply -----------------------------------------------------------
    from src.db.writer import log_step, upsert_satellite_attribution

    run_id = str(uuid.uuid4())
    started = time.monotonic()
    try:
        updated = upsert_satellite_attribution(built)
    except Exception as exc:
        log_step(run_id, pipeline="catalog_enrich", step="write_db",
                 status="failed", message=str(exc), source="celestrak_satcat")
        raise

    elapsed = time.monotonic() - started
    log_step(run_id, pipeline="catalog_enrich", step="write_db",
             status="success", records_processed=updated,
             duration_s=elapsed, source="celestrak_satcat")

    # The gap between what SATCAT knows and what we track is the number
    # worth printing. Reporting only "updated N" would make a catalogue
    # covering a quarter of the objects look like a complete pass.
    unmatched = len(built) - updated
    print(f"\nEnriched {updated:,} satellites in {elapsed:,.1f}s")
    print(f"  SATCAT records with no matching row here: {unmatched:,}")
    print(f"  ({unmatched * 100.0 / max(len(built), 1):.1f}% of SATCAT is "
          f"outside this catalogue)")
    print("\nThis pass never creates satellites. Expanding the catalogue "
          "is a")
    print("separate decision - see the note in "
          "writer.upsert_satellite_attribution.")
    print("\nVerify with:  python check_catalog.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
