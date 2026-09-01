"""
Is the ingestion pipeline actually alive?

    python check_pipeline.py

Answers the question that has been open since the 2026-08-30 restart:
is data still flowing into Supabase, or has the scheduled job been dead
for weeks? Read-only - runs SELECTs, writes nothing.

Exit codes:  0 fresh   1 stale   2 no data   3 could not connect
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from sqlalchemy import create_engine, text
except ImportError:
    print("sqlalchemy not installed in this environment:")
    print("  pip install sqlalchemy psycopg2-binary")
    sys.exit(3)

URL = os.environ.get("DATABASE_URL")
if not URL:
    print("DATABASE_URL not set - check .env")
    sys.exit(3)

TABLES = ["satellites", "tle_history", "orbital_positions", "countries",
          "sensors", "visibility_windows", "imagery_scenes", "ingestion_log"]


def main() -> int:
    try:
        engine = create_engine(URL, pool_pre_ping=True,
                               connect_args={"connect_timeout": 20})
        conn = engine.connect()
    except Exception as exc:
        print(f"Could not connect: {type(exc).__name__}: {str(exc)[:200]}")
        return 3

    # Each probe runs in its own transaction: one bad column name must not
    # poison every query that follows (it did, on the first run).
    def q(sql):
        try:
            with engine.connect() as c:
                return c.execute(text(sql)).fetchall()
        except Exception as exc:
            return ("ERR", str(exc).split("\n")[0][:70])

    print("\n  Row counts")
    print("  " + "-" * 52)
    counts = {}
    for tbl in TABLES:
        r = q(f"SELECT count(*) FROM {tbl}")
        if isinstance(r, tuple):
            print(f"    {tbl:<22}{'ERR':>10}  {r[1]}")
        else:
            counts[tbl] = r[0][0]
            print(f"    {tbl:<22}{counts[tbl]:>12,}")

    # ── liveness: when did OUR job last run? ─────────────────────────────────
    # fetched_at is written by us; epoch belongs to the element set and comes
    # from CelesTrak. Only fetched_at answers "is the pipeline running".
    print("\n  Pipeline activity (fetched_at = when our job ran)")
    print("  " + "-" * 52)
    newest = None
    for label, sql in [
        ("newest tle_history.fetched_at", "SELECT max(fetched_at) FROM tle_history"),
        ("newest satellites.last_updated", "SELECT max(last_updated) FROM satellites"),
        ("newest ingestion_log.created_at", "SELECT max(created_at) FROM ingestion_log"),
    ]:
        r = q(sql)
        if isinstance(r, tuple):
            print(f"    {label:<32} ERR {r[1][:34]}")
        elif r[0][0]:
            v = r[0][0]
            print(f"    {label:<32} {v}")
            if newest is None or v > newest:
                newest = v

    # ── data sanity: element-set epochs ──────────────────────────────────────
    print("\n  TLE epoch sanity")
    print("  " + "-" * 52)
    r = q("SELECT min(epoch), max(epoch), "
          "count(*) FILTER (WHERE epoch > now() + interval '1 day') "
          "FROM tle_history")
    if isinstance(r, tuple):
        print(f"    ERR {r[1]}")
    else:
        lo, hi, future = r[0]
        print(f"    oldest epoch          {lo}")
        print(f"    newest epoch          {hi}")
        print(f"    dated >1d in future   {future:,}")
        if future:
            print("    ^ TLE epochs should never be in the future.")
            print("      Either this machine's clock is behind, or the")
            print("      fetcher is mis-parsing the OMM EPOCH field.")

    print("\n  Last 5 ingestion_log entries")
    print("  " + "-" * 52)
    r = q("SELECT created_at, pipeline, step, status, records_processed, message "
          "FROM ingestion_log ORDER BY created_at DESC LIMIT 5")
    if isinstance(r, tuple):
        print(f"    ERR {r[1]}")
    elif not r:
        print("    (empty - the pipeline has never logged a run)")
    else:
        for row in r:
            print(f"    {str(row[0])[:19]}  {str(row[1])[:12]:<12} {str(row[2])[:10]:<10} "
                  f"{str(row[3])[:7]:<7} {str(row[4] or ''):>7}  {str(row[5] or '')[:28]}")

    # ── verdict ──────────────────────────────────────────────────────────────
    print("\n  " + "=" * 44)
    if not counts.get("satellites"):
        print("  VERDICT: NO DATA - the pipeline has never successfully run.")
        return 2

    if newest is None:
        print("  VERDICT: data present but no timestamps to judge freshness.")
        return 1

    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - newest
    hrs = age.total_seconds() / 3600

    if hrs < -1:
        print(f"  Newest activity is {abs(hrs):.1f}h in the FUTURE.")
        print("  VERDICT: CLOCK MISMATCH - this machine's clock disagrees with")
        print("  the database. Check the system time before trusting anything")
        print("  above. Cannot judge freshness.")
        return 1

    print(f"  Newest pipeline activity: {hrs:.1f}h ago.")
    if age < timedelta(hours=6):
        print("  VERDICT: ALIVE - the 2-hourly cron is running.")
        return 0
    print(f"  VERDICT: STALE - nothing for {age.days}d {hrs % 24:.0f}h.")
    print("  -> GitHub > repo > Actions tab. A scheduled workflow is")
    print("     auto-disabled after 60 days of repo inactivity; re-enable it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
