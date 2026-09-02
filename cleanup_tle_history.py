"""
One-off: bring the database back under the 500 MB tier.

Measured 2026-09-01 - 597 MB total, of which tle_history is 515 MB:

    tle_history          514.8 MB   1,574,305 rows, 327 bytes each
    visibility_windows    47.7 MB   one day
    orbital_positions     17.5 MB
    satellites            16.9 MB

The tle_history rows are real. 1-3 new element sets per satellite per
day is genuine CelesTrak churn, not the same set fetched repeatedly, so
nothing here is a bug being papered over - the table simply had no
retention policy while the other two did.

Three steps, smallest blast radius first:

  1. DROP INDEX idx_tle_history_norad. Redundant: the unique constraint
     on (norad_id, epoch) already leads with norad_id, so it serves every
     query the standalone index does. Frees 13.6 MB, touches no rows, and
     is instantly reversible.

  2. DELETE rows epoched more than --days ago. 14 days keeps 346,462 of
     1,574,305 rows.

  3. VACUUM FULL tle_history. This is the step that matters and the one
     easy to skip: DELETE only marks rows dead. Their space stays in the
     file, and Supabase bills on the file. Without this the prune shows
     up as zero reclaimed and the tier stays full.

VACUUM FULL takes an ACCESS EXCLUSIVE lock - nothing can read or write
the table while it runs - and rewrites it into a new file, so it briefly
needs room for both copies. On a 515 MB table expect a minute or two.
Do not run it while the TLE workflow is mid-write.

Dry run by default. Nothing is deleted without --apply.
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

REDUNDANT_INDEX = "idx_tle_history_norad"


def _vacuum(engine) -> None:
    """
    VACUUM FULL, which cannot run inside a transaction block.

    The obvious spelling does not work: engine.raw_connection() hands back
    SQLAlchemy's pool proxy, and setting .autocommit on that sets an
    attribute on the proxy rather than on the psycopg2 connection
    underneath, so the statement still arrives inside the implicit
    transaction and Postgres refuses it with ActiveSqlTransaction. Ask
    SQLAlchemy for the isolation level instead and it configures the real
    connection.
    """
    print("VACUUM FULL tle_history ... (locks the table; a minute or two)")
    with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("VACUUM FULL ANALYZE tle_history")


def _sizes(conn):
    total = conn.execute(text(
        "SELECT pg_total_relation_size('tle_history')")).scalar()
    rows = conn.execute(text("SELECT count(*) FROM tle_history")).scalar()
    return int(total), int(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14,
                    help="Keep element sets from the last N days (default 14)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually make the changes (default: dry run)")
    ap.add_argument("--vacuum-only", action="store_true",
                    help="Reclaim space from an earlier prune. Use this when "
                         "the DELETE committed but the VACUUM did not - the "
                         "rows are gone but the size has not moved.")
    ap.add_argument("--skip-vacuum", action="store_true",
                    help="Prune without reclaiming the space. Only useful if "
                         "you intend to run VACUUM FULL yourself later - "
                         "until it runs, the reported size will not move.")
    args = ap.parse_args(argv)

    engine = get_engine()

    if args.vacuum_only:
        with engine.connect() as conn:
            before, rows_before = _sizes(conn)
        print(f"\ntle_history: {before/1e6:,.0f} MB, {rows_before:,} live rows")
        _vacuum(engine)
        _report(engine, before)
        return 0

    with engine.connect() as conn:
        before, rows_before = _sizes(conn)
        keep = conn.execute(text(
            "SELECT count(*) FROM tle_history "
            "WHERE epoch >= now() - make_interval(days => :d)"),
            {"d": args.days}).scalar()
        idx = conn.execute(text(
            "SELECT pg_relation_size(indexname::regclass) FROM pg_indexes "
            "WHERE tablename = 'tle_history' AND indexname = :i"),
            {"i": REDUNDANT_INDEX}).scalar()

    doomed = rows_before - keep
    print(f"\ntle_history: {before/1e6:,.0f} MB, {rows_before:,} rows")
    print(f"  keep last {args.days} days:  {keep:,} rows")
    print(f"  delete:                {doomed:,} rows "
          f"({doomed*100.0/max(rows_before,1):.0f}%)")
    print(f"  drop {REDUNDANT_INDEX}: "
          f"{(idx or 0)/1e6:.1f} MB")
    print(f"  projected after vacuum: "
          f"~{(before - (idx or 0)) * keep / max(rows_before,1) / 1e6:,.0f} MB")

    if not args.apply:
        print("\nDry run. Re-run with --apply to make these changes.\n")
        return 0

    # DDL and DELETE are transactional; VACUUM FULL is not and must run
    # with autocommit, so it is done on a separate connection below.
    with engine.begin() as conn:
        if idx:
            conn.execute(text(f"DROP INDEX IF EXISTS {REDUNDANT_INDEX}"))
            print(f"\nDropped {REDUNDANT_INDEX}.")
        deleted = conn.execute(text(
            "DELETE FROM tle_history "
            "WHERE epoch < now() - make_interval(days => :d)"),
            {"d": args.days}).rowcount
        print(f"Deleted {deleted:,} rows.")

    if args.skip_vacuum:
        print("\nSkipped VACUUM FULL. The space is not reclaimed yet and "
              "the reported size will not have moved.\n")
        return 0

    _vacuum(engine)

    _report(engine, before)
    return 0


def _report(engine, before: int) -> None:
    with engine.connect() as conn:
        after, rows_after = _sizes(conn)
        total = conn.execute(text("""
            SELECT sum(pg_total_relation_size(c.oid))
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
        """)).scalar()

    print(f"\ntle_history: {before/1e6:,.0f} MB -> {after/1e6:,.0f} MB "
          f"({rows_after:,} rows)")
    print(f"database total: {int(total)/1e6:,.0f} MB of 500 MB\n")


if __name__ == "__main__":
    sys.exit(main())
