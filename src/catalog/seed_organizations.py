"""
Load GCAT's organisation table - the spine of the company picture.

    python -m src.catalog.seed_organizations --survey
    python -m src.catalog.seed_organizations --apply

Reads `data/seed/orgs.tsv`, already on disk since 2026-08-26 and already
used by `seed_gcat.py` to resolve owner codes into names. The same file,
read properly this time: 4,109 organisations with a stable code, three
names, a type, a class, a state, a location and a parent.

WHY THIS RATHER THAN WIT (reversing AD-044)
===========================================
WIT was named the Phase 3 company source. It is good at what it is good
at - websites, relationships, curation - and it is not the right spine.
GCAT's org table is complete, exact-keyed, CC-BY, needs no account and no
rate limit, and is sitting in the repository.

Measured against the GCAT catalogue on 2026-09-14: 74,059 objects with a
NORAD id across 1,641 distinct owner codes, and **90% of those objects
belong to just 63 codes**. That concentration is the argument. Enriching
sixty-three organisations moves nine objects in ten, while the remaining
1,578 stay named and attributable instead of being an unattributed tail.

WIT enriches these rows afterwards. A curated list is worth more when it
has something to attach to.

THREE NAMES, AND THEY ARE NOT INTERCHANGEABLE
=============================================
    Name       100% - transliterated native form
    EName       34% - populated precisely where Name is not English
    ShortName  100% - the recognisable handle

`satellites.operator` currently holds `Name`, so CAST reads as "Zhongguo
kongjian jishu yanjiu yuan". All three are stored; `display_name` is
generated in the database as English-then-native, so the display decision
lives in one place and can change without a re-import.

THIS SCRIPT DOES NOT TOUCH `satellites`
=======================================
Populating `operator_code` and re-resolving `operator` are changes to
17,457 existing rows. They belong to seed_gcat.py, after a survey that
writes down the predicted effect first. Loading the organisations is
additive and independent; doing it separately keeps the risky change
small and reviewable on its own.
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.env import bootstrap

bootstrap()

from sqlalchemy import text                                   # noqa: E402

from src.db.writer import get_engine, log_step                # noqa: E402

DEFAULT_ORGS = "data/seed/orgs.tsv"

#: Exactly the columns GCAT documents, in order. Asserted rather than
#: assumed: fetch_gcat_catalog() already carries a column-drift warning
#: because GCAT has reordered columns before. That warning is the
#: precedent, not a hypothetical.
EXPECTED_HEADER = [
    "Code", "UCode", "StateCode", "Type", "Class", "TStart", "TStop",
    "ShortName", "Name", "Location", "Longitude", "Latitude", "Error",
    "Parent", "ShortEName", "EName", "UName",
]

#: An exact primary key taken verbatim from a curated source. The same
#: standing as SATCAT's norad_id join: matched exactly or not at all.
GCAT_CONFIDENCE = 0.95


def _clean(v: "str | None") -> "str | None":
    """GCAT writes '-' for absent. Empty and '-' both mean nothing."""
    if v is None:
        return None
    v = v.strip()
    return None if v in ("", "-") else v


def _float(v: "str | None") -> "float | None":
    v = _clean(v)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_orgs(path: Path) -> list:
    if not path.exists():
        raise SystemExit(
            f"orgs.tsv not found: {path}\n"
            f"It ships with the repository. See data/seed/README.md.")

    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        rows = csv.reader(fh, delimiter="\t")
        header = [h.strip().lstrip("#") for h in next(rows)]
        if header != EXPECTED_HEADER:
            raise SystemExit(
                "orgs.tsv header is not what this importer was written "
                f"against.\n  expected: {EXPECTED_HEADER}\n"
                f"  found   : {header}\n\n"
                "GCAT has reordered columns before, which is why this is "
                "checked rather than assumed. Re-read the file and update "
                "EXPECTED_HEADER deliberately.")
        idx = {h: i for i, h in enumerate(header)}
        out = []
        for r in rows:
            if len(r) < len(header):
                continue
            code = _clean(r[idx["Code"]])
            if not code:
                continue
            out.append({
                "code": code,
                "ucode": _clean(r[idx["UCode"]]),
                "name_native": _clean(r[idx["Name"]]),
                "name_english": _clean(r[idx["EName"]]),
                "name_short": _clean(r[idx["ShortName"]]),
                "org_type": _clean(r[idx["Type"]]),
                "org_class": _clean(r[idx["Class"]]),
                "state_code": _clean(r[idx["StateCode"]]),
                "location": _clean(r[idx["Location"]]),
                "longitude": _float(r[idx["Longitude"]]),
                "latitude": _float(r[idx["Latitude"]]),
                "parent_code": _clean(r[idx["Parent"]]),
            })
    # name_native is NOT NULL in 011. A row without one cannot be stored,
    # and dropping it silently is how a count stops matching a file.
    nameless = [o["code"] for o in out if not o["name_native"]]
    if nameless:
        print(f"  {len(nameless)} organisation(s) have no Name and will be "
              f"skipped: {', '.join(nameless[:8])}")
        out = [o for o in out if o["name_native"]]
    return out


def survey(conn, orgs: list) -> None:
    print(f"  orgs.tsv           : {len(orgs)} organisations")

    have = {r[0] for r in conn.execute(text("SELECT code FROM organizations"))}
    new = [o for o in orgs if o["code"] not in have]
    print(f"  already loaded     : {len(have)}")
    print(f"  new                : {len(new)}")
    print(f"  would be updated   : {len(orgs) - len(new)}")

    eng = sum(1 for o in orgs if o["name_english"])
    short = sum(1 for o in orgs if o["name_short"])
    parent = sum(1 for o in orgs if o["parent_code"])
    geo = sum(1 for o in orgs if o["latitude"] is not None)
    print(f"\n  name_english       : {eng} ({100*eng/len(orgs):.0f}%) "
          f"- present where the native name is not English")
    print(f"  name_short         : {short} ({100*short/len(orgs):.0f}%)")
    print(f"  parent_code        : {parent} ({100*parent/len(orgs):.0f}%)")
    print(f"  has coordinates    : {geo} ({100*geo/len(orgs):.0f}%)")

    # Report the shape, not just the count (AD-049). A type histogram says
    # what this table can actually answer.
    types = collections.Counter(
        (o["org_type"] or "?").split("/")[0] for o in orgs)
    print("\n  primary type (first element of the compound):")
    for t, c in types.most_common(10):
        print(f"      {t:<6}{c:>6}  {'#' * min(40, c // 25)}")

    # Parents that do not resolve inside the file - the reason parent_code
    # is deliberately not a foreign key in 011.
    codes = {o["code"] for o in orgs}
    dangling = {o["parent_code"] for o in orgs
                if o["parent_code"] and o["parent_code"] not in codes}
    print(f"\n  parents not present in this file: {len(dangling)}")
    if dangling:
        print(f"      {', '.join(sorted(dangling)[:10])}")
        print("      (this is why parent_code is not a foreign key)")

    print("\n  the names this changes, for the operators that matter:")
    print(f"      {'code':<8}{'short':<14}{'native (stored today)':<40}english")
    for code in ("CAST", "JAXA", "SPX", "PLAN", "ESA", "ISRO", "CNSA"):
        o = next((x for x in orgs if x["code"] == code), None)
        if o:
            print(f"      {o['code']:<8}{(o['name_short'] or '-')[:13]:<14}"
                  f"{(o['name_native'] or '-')[:39]:<40}"
                  f"{(o['name_english'] or '-')[:30]}")


def apply(conn, orgs: list) -> int:
    now = datetime.now(timezone.utc)
    written = 0
    for o in orgs:
        conn.execute(text("""
            INSERT INTO organizations
                (code, ucode, name_native, name_english, name_short,
                 org_type, org_class, state_code, location,
                 longitude, latitude, parent_code,
                 data_source, match_method, source_confidence,
                 matched_at, updated_at)
            VALUES
                (:code, :ucode, :name_native, :name_english, :name_short,
                 :org_type, :org_class, :state_code, :location,
                 :longitude, :latitude, :parent_code,
                 'gcat_orgs', 'code', :conf, :now, :now)
            ON CONFLICT (code) DO UPDATE SET
                ucode             = EXCLUDED.ucode,
                name_native       = EXCLUDED.name_native,
                name_english      = EXCLUDED.name_english,
                name_short        = EXCLUDED.name_short,
                org_type          = EXCLUDED.org_type,
                org_class         = EXCLUDED.org_class,
                state_code        = EXCLUDED.state_code,
                location          = EXCLUDED.location,
                longitude         = EXCLUDED.longitude,
                latitude          = EXCLUDED.latitude,
                parent_code       = EXCLUDED.parent_code,
                data_source       = EXCLUDED.data_source,
                match_method      = EXCLUDED.match_method,
                source_confidence = EXCLUDED.source_confidence,
                matched_at        = EXCLUDED.matched_at,
                updated_at        = EXCLUDED.updated_at
        """), {**o, "conf": GCAT_CONFIDENCE, "now": now})
        written += 1
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orgs", default=os.environ.get("GCAT_ORGS", DEFAULT_ORGS))
    ap.add_argument("--survey", action="store_true",
                    help="Report what would change, then exit.")
    ap.add_argument("--apply", action="store_true",
                    help="Write. Without this nothing is written.")
    args = ap.parse_args(argv)

    orgs = load_orgs(Path(args.orgs))
    print(f"orgs.tsv - header verified, {len(orgs)} organisations\n")

    engine = get_engine()
    with engine.connect() as conn:
        survey(conn, orgs)

    if not args.apply:
        print("\nNothing written. Re-run with --apply.")
        return 0

    run_id = str(uuid.uuid4())
    try:
        with engine.begin() as conn:
            written = apply(conn, orgs)
    except Exception as exc:
        try:
            log_step(run_id, pipeline="organizations_seed", step="write_db",
                     status="failed", message=str(exc)[:500])
        except Exception as log_exc:                          # noqa: BLE001
            print(f"(could not write the failure to ingestion_log: "
                  f"{log_exc})", file=sys.stderr)
        raise
    log_step(run_id, pipeline="organizations_seed", step="write_db",
             status="success", records_processed=written,
             message=f"{written} organisations from GCAT orgs.tsv",
             source="gcat_orgs")
    print(f"\nWrote {written} organisations.")
    print("`satellites.operator_code` is still empty - that is seed_gcat's "
          "job, after its own survey.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
