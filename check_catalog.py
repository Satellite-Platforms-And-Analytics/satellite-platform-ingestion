"""
What does the catalogue actually know, and who told it?

Phase 2 fills descriptive columns - operator, purpose, users, mass - that
001_core_schema.sql defined on 2026-07-10 and nothing has ever written.
CelesTrak's OMM feed supplies name, catalogue number, international
designator and orbital elements. It says nothing about who owns a thing
or what it is for, so before the first enrichment pass every one of those
columns should be empty.

This script is two things at once.

  * The verification for 004_catalog_provenance.sql. Run before the first
    matcher pass it should report ~18,000 satellites and zero attributed.
    A non-zero count means something already wrote attribution, and both
    the migration's assumptions and the matcher's need re-checking before
    anything else runs.

  * The progress meter for the rest of Phase 2. Run after each enrichment
    pass it reports coverage by source, by match method and by confidence.

The check worth having is the last one. `operator = 'SpaceX'` with
data_source NULL is a value nobody can account for: it did not come from
the matcher, so it cannot be re-examined, re-scored or rolled back with
the rest of its source. Attribution without provenance is precisely the
failure mode 004 exists to prevent, so this script exits non-zero on it -
and on a missing 004 column, because a guard that skips when the thing it
guards is absent is the shape that has already cost this project seven
separate outages.

Read-only. Writes nothing, deletes nothing.

    python check_catalog.py
    python check_catalog.py --sample 20   # show unenriched rows
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

from src.db.writer import get_engine

# Defined by 001_core_schema.sql on 2026-07-10, ahead of any data.
DESCRIPTIVE = [
    "operator", "manufacturer", "purpose", "users", "country_code",
    "orbit_type", "launch_date", "launch_site", "launch_vehicle",
    "expected_lifetime_yr", "mass_kg", "perigee_km", "apogee_km",
    "inclination_deg", "period_min", "rcs_size", "status", "object_type",
]

# Added by 004_catalog_provenance.sql.
PROVENANCE = ["users", "data_source", "match_method",
              "source_confidence", "matched_at"]

INDEXES = ["idx_satellites_unenriched", "idx_satellites_operator",
           "idx_satellites_users"]


def q(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Print N unenriched satellites - the matcher's input")
    args = ap.parse_args(argv)

    problems: list[str] = []

    with get_engine().connect() as conn:

        # -- Does 004 exist? -------------------------------------------
        # Asked first because every number below is meaningless if the
        # columns are absent, and a report of zeroes reads the same
        # whether nothing is enriched or nothing can be.
        present = {r[0] for r in q(conn, """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'satellites'
        """)}
        missing = [c for c in PROVENANCE if c not in present]
        if missing:
            problems.append(
                "004_catalog_provenance.sql has not been applied - missing "
                + ", ".join(missing))
            print("\n!! satellites is missing " + ", ".join(missing))
            print("   Apply it first:")
            print("     python apply_migration.py 004_catalog_provenance.sql")
            print("   (in satellite-platform-infrastructure)")
            return 1

        have_idx = {r[0] for r in q(conn, """
            SELECT indexname FROM pg_indexes WHERE tablename = 'satellites'
        """)}
        for name in INDEXES:
            if name not in have_idx:
                problems.append(f"index {name} is missing")

        total = q(conn, "SELECT count(*) FROM satellites")[0][0]
        print(f"\nsatellites: {total:,} rows")

        # -- Coverage --------------------------------------------------
        # One count per descriptive column. Before the first enrichment
        # pass these are the baseline: whatever is non-zero here came
        # from CelesTrak, and everything else is Phase 2's to fill.
        cols = [c for c in DESCRIPTIVE if c in present]
        counts = q(conn, "SELECT " + ", ".join(
            f"count({c})" for c in cols) + " FROM satellites")[0]

        print("\nDescriptive coverage:")
        for col, n in zip(cols, counts):
            pct = n * 100.0 / max(total, 1)
            bar = "#" * int(pct / 4)
            print(f"  {col:<22}{n:>9,}{pct:>7.1f}%  {bar}")

        populated = [c for c, n in zip(cols, counts) if n]
        if populated:
            print("\n  populated: " + ", ".join(populated))
        else:
            print("\n  Nothing populated. Expected before the first "
                  "enrichment pass -")
            print("  CelesTrak supplies elements, not attribution.")

        # -- Provenance ------------------------------------------------
        enriched = q(conn,
                     "SELECT count(data_source) FROM satellites")[0][0]
        print(f"\nEnriched: {enriched:,} of {total:,} "
              f"({enriched*100.0/max(total,1):.1f}%)")

        if enriched:
            print("\n  By source:")
            for src, n, lo, hi in q(conn, """
                SELECT data_source, count(*),
                       min(matched_at), max(matched_at)
                  FROM satellites WHERE data_source IS NOT NULL
                 GROUP BY 1 ORDER BY 2 DESC
            """):
                print(f"    {src:<14}{n:>9,}   {lo} .. {hi}")

            print("\n  By match method:")
            for method, n, avg in q(conn, """
                SELECT match_method, count(*), avg(source_confidence)
                  FROM satellites WHERE data_source IS NOT NULL
                 GROUP BY 1 ORDER BY 2 DESC
            """):
                print(f"    {method or '(none)':<14}{n:>9,}   "
                      f"mean confidence {avg or 0:.2f}")

            # The convention lives in 004's COMMENT ON COLUMN: 1.0
            # norad_id, 0.9 intl_designator, 0.7 exact name, below that
            # fuzzy. The interesting number is the bottom bucket - those
            # are the rows a false positive would be hiding in.
            print("\n  By confidence:")
            for label, lo, hi in (("1.0  exact norad", 1.0, 1.01),
                                  ("0.9  designator", 0.9, 1.0),
                                  ("0.7  exact name", 0.7, 0.9),
                                  ("<0.7 fuzzy     ", 0.0, 0.7)):
                n = q(conn, """
                    SELECT count(*) FROM satellites
                     WHERE source_confidence >= :lo
                       AND source_confidence < :hi
                """, lo=lo, hi=hi)[0][0]
                print(f"    {label}{n:>9,}")

            unscored = q(conn, """
                SELECT count(*) FROM satellites
                 WHERE data_source IS NOT NULL
                   AND source_confidence IS NULL
            """)[0][0]
            if unscored:
                problems.append(
                    f"{unscored:,} enriched rows carry no confidence score")

        # -- The integrity check ---------------------------------------
        # Attribution that no source claims. Either something wrote these
        # columns outside the enrichment path, or a matcher run failed
        # part-way and left values without stamping provenance. Both are
        # worth knowing before another pass writes over them.
        #
        # Every column in DESCRIPTIVE counts, with no exemption for the
        # ingestion path: fetcher.py builds its satellite rows from
        # norad_id, name, intl_designator, orbit_regime, the TLE lines,
        # mean_motion, eccentricity and source, and touches none of these.
        # If that ever changes, this check will say so rather than the
        # change passing unnoticed.
        attributed = " OR ".join(f"{c} IS NOT NULL" for c in cols)
        orphans = q(conn, f"""
            SELECT count(*) FROM satellites
             WHERE data_source IS NULL AND ({attributed})
        """)[0][0]
        if orphans:
            problems.append(
                f"{orphans:,} rows carry attribution with no data_source")
            print(f"\n!! {orphans:,} rows have descriptive values but no "
                  f"provenance.")
            print("   These cannot be re-examined or rolled back with a "
                  "source.")
            for norad, name, op, pur in q(conn, f"""
                SELECT norad_id, name, operator, purpose
                  FROM satellites
                 WHERE data_source IS NULL AND ({attributed})
                 LIMIT 5
            """):
                print(f"     {norad:>7}  {(name or '')[:28]:<28} "
                      f"{(op or '-')[:16]:<16} {(pur or '-')[:16]}")

        # -- The working set -------------------------------------------
        # What idx_satellites_unenriched exists to serve — but split,
        # because part of it can never be enriched.
        #
        # An object with no international designator has not been
        # catalogued. SATCAT describes catalogued objects, so it has
        # nothing to say about these by definition, and no amount of
        # re-running fixes that. Counting them as pending work would
        # leave a permanent backlog that reads as a todo: on 2026-09-05
        # all 576 unenriched rows were NORAD 270xxx analyst tracks named
        # UNKNOWN, and every future pass would have reported them again.
        uncatalogued = q(conn, """
            SELECT count(*) FROM satellites
             WHERE data_source IS NULL
               AND (intl_designator IS NULL OR intl_designator = '')
        """)[0][0]
        pending = (total - enriched) - uncatalogued

        print(f"\nUnenriched: {total - enriched:,} rows")
        print(f"  awaiting enrichment:        {pending:>7,}")
        print(f"  uncatalogued / analyst:     {uncatalogued:>7,}   "
              f"no international designator")
        if uncatalogued and not pending:
            print("\n  Everything enrichable has been enriched. The "
                  "remainder are objects")
            print("  being tracked but not yet catalogued — SATCAT cannot "
                  "describe them,")
            print("  and they are the leading edge of new material in "
                  "orbit.")
        if args.sample:
            print(f"\n  First {args.sample} unenriched:")
            for norad, name, intl in q(conn, """
                SELECT norad_id, name, intl_designator
                  FROM satellites WHERE data_source IS NULL
                 ORDER BY norad_id DESC LIMIT :n
            """, n=args.sample):
                print(f"    {norad:>7}  {intl or '-':<12} {name}")

    print()
    if problems:
        print("PROBLEMS")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("OK - schema present, no attribution without provenance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
