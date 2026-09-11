"""
Copy history out of Supabase onto local disk, one whole day at a time.

    python archive_to_local.py --status
    python archive_to_local.py --dry-run
    python archive_to_local.py --apply
    python archive_to_local.py --apply --table visibility_windows

READ-ONLY against the database. This script never deletes, updates or
truncates anything. It exists so that deleting becomes safe later, and
keeping those two jobs in separate scripts is deliberate.

Why this exists
---------------
The free tier is 500 MB and the three history tables push ~93 MB/day
through it:

    visibility_windows   259 MB   222,244 rows/day    292 bytes/row
    tle_history          130 MB    24,724 rows/day    351 bytes/row
    orbital_positions     59 MB    71,420 rows/day    277 bytes/row

Nothing reads any of them. The frontend's only API route queries
`satellites` for five columns; no module in src/tracking/ so much as
mentions DATABASE_URL. They are the warehouse, and they are living inside
the serving layer. D: has 1.3 TB.

Whole days, and never today
---------------------------
Each partition is one calendar day (UTC) of one table, written once and
never rewritten. Today's partition is deliberately skipped: it is still
being written to, and a partition that is half a day's rows but marked
complete is worse than no partition at all. So `--apply` on any given run
archives everything up to and including yesterday.

This matches how the prune already works. prune_old_visibility_windows
deletes on analysis_date "so a whole run leaves together and no day is
left half-deleted" - the archive uses the same grain, so a day is either
wholly on disk or wholly not.

Verification
------------
A partition counts as archived only when the rows written to disk match
the rows the database reports for that day. The manifest records the
count, so a later run can re-check rather than trust a filename. A
partition that fails verification is deleted from disk and left
unrecorded, so the next run retries it.

Format
------
Parquet when pyarrow is available, gzipped CSV otherwise. Both are read
by DuckDB and pandas without a server; the manifest records which was
used per partition so a mixed archive stays readable.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import socket
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

# table -> (time column, whether that column is a DATE rather than a
# timestamptz). The distinction matters: DATE compares cleanly against a
# day, a timestamptz needs an explicit half-open range or rows land in
# two partitions.
TABLES = {
    "visibility_windows": ("analysis_date", True),
    "orbital_positions": ("timestamp", False),
    "tle_history": ("epoch", False),
}

MANIFEST_NAME = "manifest.json"


# ---------------------------------------------------------------- manifest

def manifest_path(root: Path) -> Path:
    return root / MANIFEST_NAME


def load_manifest(root: Path) -> dict:
    p = manifest_path(root)
    if not p.exists():
        return {"version": 1, "partitions": {}}
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        # Refuse to run rather than silently start a fresh manifest: an
        # empty manifest would re-archive everything and, worse, would
        # make a later watermark look further behind than it is.
        raise SystemExit(f"manifest at {p} is unreadable: {exc}")
    data.setdefault("partitions", {})
    return data


def save_manifest(root: Path, data: dict) -> None:
    # Write beside the target and rename: a manifest truncated by an
    # interrupted write is the one file that cannot be reconstructed.
    p = manifest_path(root)
    tmp = p.with_suffix(".json.tmp")
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["host"] = socket.gethostname()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)


def key(table: str, day: date) -> str:
    return f"{table}/{day.isoformat()}"


# ------------------------------------------------------------------ writing

def _have_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


def write_partition(rows, columns, path_base: Path, use_parquet: bool) -> tuple[Path, str]:
    """Write rows to disk. Returns (path, format)."""
    path_base.parent.mkdir(parents=True, exist_ok=True)
    if use_parquet:
        import pandas as pd
        out = path_base.with_suffix(".parquet")
        pd.DataFrame(rows, columns=columns).to_parquet(out, index=False)
        return out, "parquet"

    import csv
    out = path_base.with_suffix(".csv.gz")
    with gzip.open(out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        w.writerows(rows)
    return out, "csv.gz"


def count_rows_on_disk(path: Path, fmt: str) -> int:
    if fmt == "parquet":
        import pyarrow.parquet as pq
        return pq.ParquetFile(path).metadata.num_rows
    # csv.reader, not a line count: a quoted field may contain a newline,
    # and a physical-line count would then read high and fail a partition
    # that is actually correct.
    import csv
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        return max(sum(1 for _ in csv.reader(fh)) - 1, 0)   # minus header


# ------------------------------------------------------------------- queries

def days_present(conn, table: str) -> list[date]:
    col, is_date = TABLES[table]
    expr = col if is_date else f"({col} AT TIME ZONE 'UTC')::date"
    rows = conn.execute(
        text(f"SELECT DISTINCT {expr} AS d FROM {table} ORDER BY d")
    ).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def fetch_day(conn, table: str, day: date):
    col, is_date = TABLES[table]
    if is_date:
        where = f"{col} = :d"
        params = {"d": day}
    else:
        # Half-open range on the raw column so an index can be used, and
        # so a row at exactly midnight lands in one partition only.
        where = f"{col} >= :start AND {col} < :end"
        params = {
            "start": datetime.combine(day, datetime.min.time(), timezone.utc),
            "end": datetime.combine(day + timedelta(days=1),
                                    datetime.min.time(), timezone.utc),
        }
    result = conn.execute(text(f"SELECT * FROM {table} WHERE {where}"), params)
    return list(result.keys()), result.fetchall()


def count_day(conn, table: str, day: date) -> int:
    col, is_date = TABLES[table]
    if is_date:
        return conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE {col} = :d"), {"d": day}
        ).scalar_one()
    return conn.execute(
        text(f"SELECT count(*) FROM {table} "
             f"WHERE {col} >= :start AND {col} < :end"),
        {"start": datetime.combine(day, datetime.min.time(), timezone.utc),
         "end": datetime.combine(day + timedelta(days=1),
                                 datetime.min.time(), timezone.utc)},
    ).scalar_one()


# ---------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive-dir", default=os.environ.get(
        "SATELLITE_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR))
    ap.add_argument("--table", action="append", choices=sorted(TABLES),
                    help="Archive only this table (repeatable). "
                         "Default: all three.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write. Without it nothing is written.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Explicit no-op; the default, kept for symmetry "
                         "with the other scripts here.")
    ap.add_argument("--status", action="store_true",
                    help="Show what is archived and what is outstanding, "
                         "then exit.")
    ap.add_argument("--force-csv", action="store_true",
                    help="Write gzipped CSV even if pyarrow is available.")
    args = ap.parse_args(argv)

    root = Path(args.archive_dir)
    tables = args.table or sorted(TABLES)
    today = datetime.now(timezone.utc).date()

    use_parquet = _have_pyarrow() and not args.force_csv
    if not use_parquet and not args.force_csv:
        print("pyarrow not installed - writing gzipped CSV instead of "
              "Parquet. Both are readable by DuckDB; install pyarrow for "
              "smaller files and faster queries.\n")

    manifest = load_manifest(root) if root.exists() else {"version": 1,
                                                          "partitions": {}}
    engine = get_engine()

    total_new = 0
    with engine.connect() as conn:
        for table in tables:
            present = days_present(conn, table)
            pending = [d for d in present
                       if d < today and key(table, d) not in manifest["partitions"]]
            done = [d for d in present if key(table, d) in manifest["partitions"]]
            skipped_today = [d for d in present if d >= today]

            print(f"{table}")
            if present:
                print(f"  days in database : {len(present)}"
                      f"  ({present[0]} .. {present[-1]})")
            else:
                print("  days in database : 0")
            print(f"  already archived : {len(done)}")
            print(f"  to archive now   : {len(pending)}")
            if skipped_today:
                print(f"  skipped (today)  : "
                      f"{', '.join(str(d) for d in skipped_today)}"
                      f"  - still being written")

            if args.status or not args.apply:
                for d in pending:
                    print(f"      would archive {d}: "
                          f"{count_day(conn, table, d):,} rows")
                print()
                continue

            for d in pending:
                expected = count_day(conn, table, d)
                columns, rows = fetch_day(conn, table, d)
                if len(rows) != expected:
                    # The table changed under us mid-read. Skip rather
                    # than record a partial day; the next run retries.
                    print(f"      {d}: SKIPPED - read {len(rows):,} rows "
                          f"but count said {expected:,}. Table is being "
                          f"written. Re-run.")
                    continue

                out, fmt = write_partition(
                    rows, columns, root / table / d.isoformat(), use_parquet)

                on_disk = count_rows_on_disk(out, fmt)
                if on_disk != expected:
                    out.unlink(missing_ok=True)
                    print(f"      {d}: FAILED verification - "
                          f"{on_disk:,} on disk vs {expected:,} expected. "
                          f"File removed, not recorded.")
                    continue

                manifest["partitions"][key(table, d)] = {
                    "rows": expected,
                    "bytes": out.stat().st_size,
                    "format": fmt,
                    "file": str(out.relative_to(root)),
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                }
                save_manifest(root, manifest)     # after every partition
                total_new += 1
                ratio = out.stat().st_size / max(expected, 1)
                print(f"      {d}: {expected:,} rows -> "
                      f"{out.stat().st_size / 1e6:.1f} MB "
                      f"({ratio:.0f} bytes/row) {fmt}")
            print()

    if args.apply:
        print(f"Archived {total_new} new partition(s) to {root}")
        if total_new:
            print("\nNothing has been deleted from the database. Verify the "
                  "counts above against check_bloat.py before pruning or "
                  "truncating anything.")
    else:
        print("Dry run - nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
