"""
Truncate a history table, but only if every row of it is already on disk.

    python truncate_archived.py --status
    python truncate_archived.py --table visibility_windows
    python truncate_archived.py --table visibility_windows --apply

The companion to archive_to_local.py, kept separate on purpose: one
script reads and one deletes, so neither can quietly do the other's job.

Why TRUNCATE and not DELETE
---------------------------
DELETE marks rows dead and returns nothing to the filesystem; Supabase
bills on the file. VACUUM FULL is what actually shrinks a table, and it
rewrites into a new file, so it needs free space roughly equal to the
table. At 97% of a 500 MB tier that space does not exist - the reclaim
tool cannot run precisely when it is most needed.

TRUNCATE frees the file immediately and needs no second copy. It is the
only lever available at high occupancy, which is why it is worth having
a script that makes it safe rather than avoiding it.

The safety property
-------------------
It refuses unless, for every distinct day present in the table, the
manifest holds a partition for that day whose row count matches the
database exactly. Comparing totals alone is not enough: two different
sets of days can sum to the same number. Per-day matching is what makes
"this table is fully archived" a fact rather than an inference.

The counts are taken at truncate time, not read from an earlier report,
so a workflow that wrote rows in between is caught rather than lost.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

from sqlalchemy import text

from src.db.writer import get_engine

DEFAULT_ARCHIVE_DIR = r"D:\Databases\satellite\archive"

# Must match archive_to_local.TABLES.
TABLES = {
    "visibility_windows": ("analysis_date", True),
    "orbital_positions": ("timestamp", False),
    "tle_history": ("epoch", False),
}

# Tables whose contents can be rebuilt from data we still hold, and so
# could in principle be truncated with a known loss. tle_history is
# absent deliberately: CelesTrak serves current elements only and
# Space-Track's GP_History is one request per lifetime, so a pruned
# element set is gone for good. --accept-loss will not touch it.
REGENERABLE = {
    "visibility_windows": "recomputed daily from TLEs by ingest_visibility",
    "orbital_positions": "recomputed from tle_history by the propagator",
}


def live_days(conn, table: str) -> dict:
    """{day: row count} straight from the database, right now."""
    col, is_date = TABLES[table]
    expr = col if is_date else f"({col} AT TIME ZONE 'UTC')::date"
    rows = conn.execute(text(
        f"SELECT {expr} AS d, count(*) FROM {table} GROUP BY d ORDER BY d"
    )).fetchall()
    return {r[0]: r[1] for r in rows if r[0] is not None}


def archived_days(manifest: dict, table: str) -> dict:
    out = {}
    for k, v in manifest.get("partitions", {}).items():
        t, _, day = k.partition("/")
        if t == table:
            out[date.fromisoformat(day)] = v["rows"]
    return out


def compare(live: dict, arch: dict) -> tuple[list, list]:
    """Returns (missing_days, mismatched_days)."""
    missing = sorted(d for d in live if d not in arch)
    mismatched = sorted(
        (d, live[d], arch[d]) for d in live
        if d in arch and live[d] != arch[d]
    )
    return missing, mismatched


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive-dir", default=os.environ.get(
        "SATELLITE_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR))
    ap.add_argument("--table", action="append", choices=sorted(TABLES),
                    help="Table to check or truncate (repeatable).")
    ap.add_argument("--status", action="store_true",
                    help="Report coverage for all three tables and exit.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually TRUNCATE. Without it, nothing changes.")
    ap.add_argument("--accept-loss", action="store_true",
                    help="Permit truncation of a regenerable table whose "
                         "current day is not archived, losing those rows. "
                         "Prints the exact count first. Never applies to "
                         "tle_history.")
    args = ap.parse_args(argv)

    root = Path(args.archive_dir)
    mpath = root / "manifest.json"
    if not mpath.exists():
        print(f"No manifest at {mpath}. Run archive_to_local.py first.")
        return 2
    with open(mpath, encoding="utf-8") as fh:
        manifest = json.load(fh)

    tables = args.table or sorted(TABLES)
    engine = get_engine()
    to_truncate = []

    with engine.connect() as conn:
        for table in tables:
            live = live_days(conn, table)
            arch = archived_days(manifest, table)
            missing, mismatched = compare(live, arch)
            live_total = sum(live.values())
            lost = sum(live[d] for d in missing)

            print(f"{table}")
            print(f"  live rows        : {live_total:,} across {len(live)} day(s)")
            print(f"  archived         : {sum(arch.get(d, 0) for d in live):,}")
            if mismatched:
                print("  MISMATCHED DAYS  :")
                for d, lv, av in mismatched:
                    print(f"      {d}: database {lv:,} vs archive {av:,}")
            if missing:
                print(f"  NOT ARCHIVED     : {lost:,} rows across "
                      f"{len(missing)} day(s): "
                      f"{', '.join(str(d) for d in missing)}")

            if mismatched:
                print("  -> REFUSING. An archived day no longer matches the "
                      "database.\n     Investigate before truncating; do not "
                      "re-archive over it.\n")
                continue

            if missing:
                if not args.accept_loss:
                    print("  -> refusing: re-run archive_to_local.py, or pass "
                          "--accept-loss\n     if losing the days above is "
                          "acceptable.\n")
                    continue
                if table not in REGENERABLE:
                    print("  -> REFUSING: --accept-loss does not apply to "
                          f"{table}. Those rows\n     cannot be re-fetched "
                          "at any sensible cost.\n")
                    continue
                print(f"  -> --accept-loss given. {lost:,} rows will be "
                      f"destroyed.\n     ({REGENERABLE[table]})")
            else:
                print("  -> fully archived; truncation loses nothing.")

            to_truncate.append((table, live_total, lost))
            print()

        if not to_truncate:
            print("Nothing eligible to truncate.")
            return 1

        if args.status or not args.apply:
            print("Would truncate: " + ", ".join(t for t, _, _ in to_truncate))
            print("Dry run - nothing changed. Re-run with --apply.")
            return 0

    # Truncate outside the read connection, one statement per table, each
    # in its own transaction so a failure on one leaves the others alone.
    for table, rows, lost in to_truncate:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table}"))
        print(f"TRUNCATED {table}: {rows:,} rows removed "
              f"({lost:,} of them unarchived). Space is returned to the "
              f"filesystem immediately; no VACUUM needed.")

    print("\nRe-run check_bloat.py to confirm the new size.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
