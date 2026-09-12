"""
Enrich the catalogue from GCAT, the second of Phase 2's three sources.

    python -m src.catalog.seed_gcat --survey
    python -m src.catalog.seed_gcat --dry-run
    python -m src.catalog.seed_gcat --apply

WHY GCAT, AND WHY SECOND
========================
`data/seed/README.md` sets the order: SATCAT, then GCAT, then UCS. Each
step makes the next safer. SATCAT filled eleven columns on an exact
norad_id join with no matching risk, giving every row a launch date. GCAT
is now checked *against* that rather than trusted alone. UCS goes last
because its fuzzy name matches can then be corroborated against a launch
date already in the row.

GCAT is Jonathan McDowell's General Catalog - CC-BY, monthly, and
independent of Space-Track. It covers objects launched since May 2023,
which is precisely what UCS structurally cannot: that database paused
updates and is frozen at 2023-05-01.

WHAT THIS PASS ADDS THAT SATCAT DID NOT
=======================================
Two columns, and both were empty before:

    deployment_date
                GCAT's LDate again, into the column 008 adds. SATCAT
                gives an ISS-deployed cubesat launch_date 1998-11-20 -
                Zarya's launch - because the designator convention hands
                it the station's designator. 532 catalogued objects are
                affected and check_new_objects.py was filing every one of
                them as `newly_visible` instead of a deployment, because
                their apparent age is ~10,000 days.

    operator    GCAT's Owner is the operating organisation (SPXS, CASC,
                GSFC - 1,892 distinct codes). SATCAT's OWNER is a
                country-level code and goes to owner_code, so `operator`
                had no source at all until now.

    orbit_type  GCAT's OpOrbit is orbital geometry - 24 values, LEO/S,
                LLEO/I, GEO/ID, GTO, MEO, HEO. seed_satcat.py deliberately
                did NOT map SATCAT's ORBIT_TYPE because that field is a
                disposition (ORB/IMP/LAN/DOC), not geometry. This is the
                column SATCAT could not fill.

Everything else GCAT carries - launch date, period, apogee, perigee,
inclination - SATCAT already supplied, so those are COALESCEd: they fill
only the rows SATCAT missed and never overwrite it. That is the whole
meaning of "supplement, not replacement".

NOT orbit_regime. That column belongs to the 2-hourly CelesTrak fetch,
and tests/test_attribution_writer.py enforces the separation.

TWO FILES, BOTH REQUIRED
========================
`currentcat.tsv` carries Owner as a *code*. Writing 'SPXS' into a
human-facing `operator` column would be misleading, so the org name table
is mandatory rather than optional:

    data/seed/orgs.tsv
    https://planet4589.org/space/gcat/tsv/tables/orgs.tsv

Downloaded by hand, like every seed file here - see
docs/API_USAGE_POLICY.md. If it is absent this script refuses rather than
writing codes, because a column that silently means something different
from its name is worse than a column that is empty.

COLUMN DRIFT IS THE KNOWN FAILURE
=================================
GCAT has reordered columns before. `satellite_utils.fetch_gcat_catalog()`
already carries a drift warning for that reason, and this script asserts
the full expected header rather than trusting positions. A silently
shifted column would map Owner onto State and enrich 18,000 rows with
nonsense that looks plausible.
"""
from __future__ import annotations

import argparse
import csv
import os
import uuid
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

from src.db.writer import get_engine, log_step, upsert_satellite_attribution

from sqlalchemy import text

#: Verified against the copy on disk, 2026-09-12. The leading '#' on the
#: first field is GCAT's comment marker and is stripped before comparison.
EXPECTED_CURRENTCAT_HEADER = [
    "JCAT", "DeepCat", "Satcat", "Piece", "Active", "Type", "Name",
    "LDate", "Parent", "Owner", "State", "SDate", "ExpandedStatus",
    "DDate", "ODate", "Period", "Perigee", "PF", "Apogee", "AF",
    "Inc", "IF", "OpOrbit",
]

#: orgs.tsv, per https://planet4589.org/space/gcat/web/orgs/
EXPECTED_ORGS_HEADER = [
    "Code", "UCode", "StateCode", "Type", "Class", "TStart", "TStop",
    "ShortName", "Name", "Location", "Longitude", "Latitude", "Error",
    "Parent", "ShortEName", "EName", "UName",
]

DEFAULT_CURRENTCAT = (
    r"C:\Users\toddl\OneDrive\Data Science Project\Satellite Project"
    r"\Satellite Visibility Tool\data\gcat_currentcat.tsv"
)

#: Between SATCAT's 1.0 and UCS's planned 0.9.
#:
#: Not 1.0: GCAT is a curated secondary catalogue, and where it disagrees
#: with SATCAT on a field both supply, SATCAT should win - which COALESCE
#: already guarantees, but the confidence number should say the same thing.
#: Not 0.9: the join is exact on catalogue number, so it carries none of
#: the false-positive risk that earns UCS its discount.
GCAT_CONFIDENCE = 0.95

#: GCAT writes months as three-letter English abbreviations.
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

_NULLS = {"", "-", "?", "*"}


def _clean(v: str) -> "str | None":
    v = (v or "").strip()
    return None if v in _NULLS else v


def parse_gcat_date(raw: str) -> "date | None":
    """
    GCAT dates: 'YYYY Mon D', optionally with a time, optionally with a
    trailing '?' marking uncertainty.

    Measured over all 82,870 rows of the copy on disk: 82,788 are the
    three-part form, 45 are empty, 32 are a bare uncertain year, 5 are
    three-part with '?'. A year-only value is returned as None rather than
    guessed to January 1st - an invented day is worse than a null, and
    `launch_date` feeds new-launch classification.
    """
    v = _clean(raw)
    if v is None:
        return None
    v = v.rstrip("?").strip()
    parts = v.split()
    if len(parts) < 3:
        return None                      # year, or 'YYYY Mon' - not a date
    year, mon, day = parts[0], parts[1], parts[2]
    if mon not in _MONTHS or not year.isdigit():
        return None
    day = day.rstrip("?")
    if not day.isdigit():
        return None
    try:
        return date(int(year), _MONTHS[mon], int(day))
    except ValueError:
        return None                      # e.g. Feb 30 in a vague record


def _float(raw: str) -> "float | None":
    v = _clean(raw)
    if v is None:
        return None
    v = v.rstrip("?")
    try:
        return float(v)
    except ValueError:
        return None


def _read_tsv(path: Path, expected: list, label: str):
    """Yield dict rows, asserting the header matches `expected` exactly."""
    if not path.exists():
        raise SystemExit(
            f"{label} not found at {path}\n"
            f"GCAT's org names live in a separate file. Download\n"
            f"  https://planet4589.org/space/gcat/tsv/tables/orgs.tsv\n"
            f"by hand into data/seed/ - see docs/API_USAGE_POLICY.md."
            if label == "orgs.tsv" else
            f"{label} not found at {path}")

    fh = path.open(encoding="utf-8", errors="replace", newline="")
    header = None
    for line in fh:
        line = line.rstrip("\n")
        if header is None:
            header = [h.strip().lstrip("#") for h in line.split("\t")]
            if header != expected:
                extra = set(header) - set(expected)
                missing = set(expected) - set(header)
                raise SystemExit(
                    f"{label} header has drifted.\n"
                    f"  expected {len(expected)} fields, got {len(header)}\n"
                    f"  missing: {sorted(missing) or 'none'}\n"
                    f"  unexpected: {sorted(extra) or 'none'}\n"
                    f"GCAT has reordered columns before. Refusing to map by "
                    f"position against a header we do not recognise - a "
                    f"shifted column enriches every row with plausible "
                    f"nonsense."
                )
            continue
        if line.startswith("#") or not line.strip():
            continue                      # '# Updated ...' and blanks
        fields = line.split("\t")
        if len(fields) != len(header):
            continue
        yield dict(zip(header, fields))
    fh.close()


def load_org_names(path: Path) -> dict:
    """
    Code -> operator name.

    Prefers `Name`, falls back to `ShortName`. A code whose row has
    neither is left unmapped rather than defaulting to the code, so the
    survey can report how many objects that affects.
    """
    names = {}
    for row in _read_tsv(path, EXPECTED_ORGS_HEADER, "orgs.tsv"):
        code = _clean(row["Code"])
        if not code:
            continue
        names[code] = _clean(row["Name"]) or _clean(row["ShortName"])
    return {k: v for k, v in names.items() if v}


def resolve_owner(code: "str | None", orgs: dict) -> "tuple[str | None, str]":
    """
    GCAT Owner code -> operator name, plus how it was resolved.

    Two forms the first version of this missed, found in the 2,084
    unresolved codes the 2026-09-12 survey reported - the top ten were
    all one or the other:

      `GSFC?`      an uncertainty marker on the code itself. Stripped;
                   the uncertainty is about the attribution, not the
                   organisation's identity, and orgs.tsv has no '?' keys.

      `NROC/CIA`   joint ownership, 'A/B'. orgs.tsv keys individual
                   organisations, so the compound never matches. Tried
                   whole first - some compounds ARE registered - then the
                   leading component, which GCAT documents as the primary
                   owner.

    A compound resolved by its first component returns 'partial', so the
    survey can report how many operators are the lead of a joint
    arrangement rather than the whole of it.
    """
    if not code:
        return None, "none"
    if code in orgs:
        return orgs[code], "exact"

    bare = code.rstrip("?").strip()
    if bare != code and bare in orgs:
        return orgs[bare], "uncertain"

    if "/" in bare:
        lead = bare.split("/", 1)[0].strip()
        if lead in orgs:
            return orgs[lead], "partial"

    return None, "unresolved"


def build_rows(currentcat: Path, orgs: dict) -> tuple:
    """
    Returns (rows, stats). One row per GCAT record with a numeric Satcat.

    Whether a row matches anything in `satellites` is the database's
    business - upsert_satellite_attribution is UPDATE-only, so a GCAT
    record for an object we do not track updates nothing and cannot
    create a phantom satellite.
    """
    rows = []
    st = Counter()
    unresolved = Counter()
    now = datetime.now(timezone.utc)

    for rec in _read_tsv(currentcat, EXPECTED_CURRENTCAT_HEADER,
                         "gcat_currentcat.tsv"):
        st["read"] += 1
        sat = _clean(rec["Satcat"])
        if not sat or not sat.isdigit():
            st["no_catalogue_number"] += 1
            continue

        owner_code = _clean(rec["Owner"])
        operator, how = resolve_owner(owner_code, orgs)
        st[f"owner_{how}"] += 1
        if how == "unresolved":
            unresolved[owner_code] += 1

        orbit_type = _clean(rec["OpOrbit"])
        launch_date = parse_gcat_date(rec["LDate"])

        if operator:
            st["operator"] += 1
        if orbit_type:
            st["orbit_type"] += 1

        rows.append({
            "norad_id": int(sat),
            # Not written to the database - `name` belongs to the 2-hourly
            # fetch. Carried so the survey can compare the two catalogues'
            # names for a row and show a mis-join as the obvious thing it
            # is, rather than leaving a date gap to be theorised about.
            "_gcat_name": _clean(rec["Name"]),
            "_gcat_piece": _clean(rec["Piece"]),
            "operator": operator,
            "orbit_type": orbit_type,
            "launch_date": launch_date,
            # The same GCAT field feeds both columns, and that is the
            # point. GCAT is object-centric: its date is when the object
            # began independent existence - the deployment for anything
            # released from a station, the launch for everything else.
            # That is deployment_date's definition exactly.
            #
            # launch_date is COALESCEd, so SATCAT's answer stands where it
            # has one and this fills only the 253 it left NULL.
            # deployment_date is empty, so this populates it outright, and
            # the two disagree precisely where the designator convention
            # makes launch_date the wrong question - 1998-11-20 for every
            # ISS cubesat.
            "deployment_date": launch_date,
            "period_min": _float(rec["Period"]),
            "inclination_deg": _float(rec["Inc"]),
            "apogee_km": _float(rec["Apogee"]),
            "perigee_km": _float(rec["Perigee"]),
            # Deliberately not set by this pass:
            #   object_type  SATCAT's is clean; GCAT's Type has 862
            #                distinct compound values ('D  P', 'P      O')
            #   country_code GCAT's State is its own code system, and the
            #                column is an FK to countries(code)
            #   status       SATCAT's ops_status_code is already there;
            #                GCAT's ExpandedStatus is free prose
            "object_type": None,
            "status": None,
            "owner_code": None,
            "country_code": None,
            "rcs_size": None,
            "launch_site": None,
            "data_source": "gcat",
            "match_method": "norad_id",
            "source_confidence": GCAT_CONFIDENCE,
            "matched_at": now,
        })
    st["rows"] = len(rows)
    return rows, st, unresolved


def survey(conn, rows: list) -> None:
    """
    What this pass would actually change, and where the two sources
    disagree.

    The first version of this reported, for each column, how many tracked
    rows GCAT *offers* a value for. That is the wrong number and it
    misled on its first run: GCAT offered 17,543 launch_dates while only
    582 rows were NULL, so the real gain was at most 582 and the printed
    figure was thirty times it. Descriptive columns are COALESCEd, so an
    offer against a filled column changes nothing.

    Worse, the offers count threw away the most useful thing available.
    ~17,000 rows have a launch date from BOTH sources, and comparing them
    is what `data/seed/README.md` means by GCAT being "checked against
    SATCAT rather than trusted alone". Agreement is evidence for both.
    Disagreement is the signal that row-level provenance is no longer
    enough and a `satellite_attribution` table keyed
    (norad_id, field, source) has earned its keep.
    """
    by_id = {r["norad_id"]: r for r in rows}
    ids = list(by_id)

    existing = list(conn.execute(text("""
        SELECT norad_id, operator, orbit_type, launch_date
          FROM satellites
         WHERE norad_id = ANY(:ids)
    """), {"ids": ids}))

    print(f"  GCAT records with a catalogue number : {len(rows):,}")
    print(f"  of those, tracked here               : {len(existing):,}")

    cols = ("operator", "orbit_type", "launch_date")
    gain = Counter()          # NULL here, value in GCAT -> a real change
    agree = Counter()         # both present, equal
    differ = Counter()        # both present, different
    offered = Counter()       # GCAT has a value at all
    examples = {c: [] for c in cols}

    for norad, *vals in existing:
        g = by_id[norad]
        for col, have in zip(cols, vals):
            want = g[col]
            if want is None:
                continue
            offered[col] += 1
            if have is None:
                gain[col] += 1
            elif have == want:
                agree[col] += 1
            else:
                differ[col] += 1
                if len(examples[col]) < 5:
                    examples[col].append((norad, have, want))

    print(f"\n  {'column':<16}{'GAINS':>8}{'agree':>9}{'DIFFER':>8}"
          f"{'offered':>9}")
    print("  " + "-" * 50)
    for col in cols:
        print(f"  {col:<16}{gain[col]:>8,}{agree[col]:>9,}"
              f"{differ[col]:>8,}{offered[col]:>9,}")
    print("\n  GAINS is the only column that changes anything. `agree` is")
    print("  two independent catalogues corroborating each other; `DIFFER`")
    print("  is the interesting case - COALESCE keeps the existing value,")
    print("  so nothing is lost, but a large number here means the sources")
    print("  genuinely conflict and per-field provenance is needed.")

    for col in cols:
        if examples[col]:
            print(f"\n  {col} disagreements (existing vs GCAT):")
            for norad, have, want in examples[col]:
                print(f"    {norad:>7}  {have!s:<24} {want}")

    _characterise_date_disagreements(existing, by_id, conn)


def _characterise_date_disagreements(existing, by_id, conn=None) -> None:
    """
    How far apart are the two catalogues, and over how many launches?

    A count of disagreements is nearly useless on its own. The first run
    of this survey reported 66 differing launch dates, and the five
    examples were consecutive catalogue numbers all off by exactly one day
    across 2023-11-03/04 - consecutive numbers mean one launch, and a
    uniform one-day offset means a launch near midnight UTC, not a data
    fault.

    So the question is not "how many rows differ" but "how far, and how
    many distinct launches". If every difference is +/-1 day the two
    sources agree to within a rounding convention, COALESCE keeping
    SATCAT is correct, and the per-field provenance table that
    004_catalog_provenance.sql anticipates has NOT yet earned its keep.
    A spread of weeks or years would say the opposite.
    """
    _db_names = {}
    deltas = Counter()
    per_delta_ids = {}
    for norad, _operator, _orbit, have in existing:
        want = by_id[norad]["launch_date"]
        if have is None or want is None or have == want:
            continue
        d = (want - have).days
        deltas[d] += 1
        per_delta_ids.setdefault(d, []).append(norad)

    if not deltas:
        return

    if conn is not None and per_delta_ids:
        far_ids = [i for d, ids in per_delta_ids.items() if abs(d) > 1
                   for i in ids]
        if far_ids:
            _db_names.update({r[0]: r[1] for r in conn.execute(text(
                "SELECT norad_id, name FROM satellites "
                "WHERE norad_id = ANY(:ids)"), {"ids": far_ids})})

    total = sum(deltas.values())
    print(f"\n  launch_date disagreement shape ({total} rows):")
    print(f"    {'GCAT - existing':>18}  {'rows':>6}  {'runs':>5}   consecutive-id runs")
    for d in sorted(deltas):
        ids = sorted(per_delta_ids[d])
        runs = 1 + sum(1 for a, b in zip(ids, ids[1:]) if b != a + 1)
        label = f"{d:+d} day" + ("s" if abs(d) != 1 else "")
        print(f"    {label:>18}  {deltas[d]:>6}  {runs:>5}")

    far = {d: ids for d, ids in per_delta_ids.items() if abs(d) > 1}
    if far:
        print("\n  rows differing by MORE than a day - every one of them:")
        print(f"    {'norad':>7}  {'ours':<11} {'GCAT':<11} {'delta':>8}"
              f"  name (ours) / name (GCAT) / GCAT designator")
        for d in sorted(far):
            for norad in sorted(far[d]):
                g = by_id[norad]
                ours = next((r for r in existing if r[0] == norad), None)
                print(f"    {norad:>7}  {ours[3]!s:<11} "
                      f"{g['launch_date']!s:<11} {d:>+8}")
                print(f"             {(_db_names.get(norad) or '?')[:34]:<34} "
                      f"| {(g['_gcat_name'] or '?')[:26]:<26} "
                      f"| {g['_gcat_piece'] or '?'}")
        print("\n    If the two names describe different objects, this is a")
        print("    JOIN fault, not a source conflict - and operator/")
        print("    orbit_type would be written from the wrong satellite,")
        print("    silently, because those columns have no existing value")
        print("    to disagree with.")

    within_one = sum(n for d, n in deltas.items() if abs(d) <= 1)
    print(f"\n    within +/-1 day: {within_one}/{total} "
          f"({within_one / total * 100:.0f}%)")
    if within_one == total:
        print("    Every difference is a single day, in consecutive-id runs:")
        print("    launches near midnight UTC, recorded either side of it by")
        print("    two conventions. The sources agree. COALESCE keeping the")
        print("    existing value is correct and no per-field provenance")
        print("    table is needed for this.")
    else:
        print("    Differences beyond a day exist. Read those rows before")
        print("    applying: this is the condition 004 names as the trigger")
        print("    for a satellite_attribution table keyed")
        print("    (norad_id, field, source).")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--currentcat", default=os.environ.get(
        "GCAT_CURRENTCAT", DEFAULT_CURRENTCAT))
    ap.add_argument("--orgs", default="data/seed/orgs.tsv")
    ap.add_argument("--survey", action="store_true",
                    help="Report coverage and what would change, then exit.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and report; write nothing.")
    ap.add_argument("--apply", action="store_true",
                    help="Write. Without this nothing is written.")
    ap.add_argument("--show-unresolved", type=int, default=0,
                    metavar="N",
                    help="List the N most common unresolved Owner codes.")
    args = ap.parse_args(argv)

    orgs = load_org_names(Path(args.orgs))
    print(f"orgs.tsv: {len(orgs):,} organisation codes resolved to names")

    rows, st, unresolved = build_rows(Path(args.currentcat), orgs)
    print(f"\ngcat_currentcat.tsv")
    print(f"  records read               : {st['read']:,}")
    print(f"  no catalogue number        : {st['no_catalogue_number']:,}"
          f"  (cannot join)")
    print(f"  usable rows                : {st['rows']:,}")
    print(f"  with an operator name      : {st['operator']:,}")
    print(f"  with an orbit_type         : {st['orbit_type']:,}")
    print(f"  owner resolved exactly     : {st['owner_exact']:,}")
    print(f"  ...after stripping '?'     : {st['owner_uncertain']:,}")
    print(f"  ...by lead of 'A/B' joint  : {st['owner_partial']:,}")
    print(f"  Owner code unresolved      : {st['owner_unresolved']:,}")
    if args.show_unresolved and unresolved:
        print("\n  most common unresolved codes:")
        for code, n in unresolved.most_common(args.show_unresolved):
            print(f"    {code:<12}{n:>7,}")

    engine = get_engine()
    with engine.connect() as conn:
        print()
        survey(conn, rows)

    if not args.apply:
        print("\nNothing written. Re-run with --apply.")
        return 0

    # uuid4, because ingestion_log.run_id is a UUID column. The first
    # version of this used a readable timestamp string, which meant the
    # *error handler* below raised InvalidTextRepresentation while trying
    # to record a different failure - and the logging error is what
    # surfaced, burying the real one. Match seed_satcat.py.
    run_id = str(uuid.uuid4())
    # The survey-only keys are not columns. upsert_satellite_attribution
    # builds its VALUES list from _ATTRIBUTION_COLUMNS, so an extra key is
    # ignored rather than fatal - dropped here anyway, because relying on
    # a writer to ignore your mistakes is how the mistake survives.
    payload = [{k: v for k, v in r.items() if not k.startswith("_")}
               for r in rows]
    try:
        updated = upsert_satellite_attribution(payload)
    except Exception as exc:
        # Logging the failure must never replace the failure. On
        # 2026-09-12 a bad run_id made this handler raise, and the UUID
        # error is what reached the terminal while the real cause - a
        # column that did not exist yet - was buried in a chained
        # traceback. An error path that can destroy the error is worse
        # than no error path.
        try:
            log_step(run_id, pipeline="catalog_enrich", step="write_db",
                     status="failed", message=f"gcat: {exc}"[:500])
        except Exception as log_exc:                       # noqa: BLE001
            print(f"(could not write the failure to ingestion_log: "
                  f"{log_exc})", file=sys.stderr)
        raise
    log_step(run_id, pipeline="catalog_enrich", step="write_db",
             status="success", records_processed=updated,
             message=f"gcat: {updated} satellites enriched from "
                     f"{len(rows)} records", source="gcat")
    print(f"\nEnriched {updated:,} satellites from {len(rows):,} GCAT "
          f"records.")
    print("Run `python check_catalog.py` to confirm no row gained a value "
          "without provenance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
