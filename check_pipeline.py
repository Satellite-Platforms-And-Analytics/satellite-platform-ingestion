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

    with conn:
        print("\n  Row counts")
        print("  " + "-" * 44)
        counts = {}
        for t in TABLES:
            try:
                counts[t] = conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                print(f"    {t:<22}{counts[t]:>10,}")
            except Exception as exc:
                print(f"    {t:<22}{'ERR':>10}  {str(exc)[:40]}")

        # Freshest TLE epoch tells us when data last actually arrived
        newest = None
        for table, col in (("tle_history", "epoch"), ("satellites", "updated_at"),
                           ("orbital_positions", "timestamp")):
            try:
                v = conn.execute(text(f"SELECT max({col}) FROM {table}")).scalar()
                if v:
                    print(f"\n    newest {table}.{col}: {v}")
                    if newest is None or v > newest:
                        newest = v
            except Exception:
                pass

        print("\n  Last 5 ingestion_log entries")
        print("  " + "-" * 44)
        try:
            rows = conn.execute(text(
                "SELECT created_at, pipeline, status, message "
                "FROM ingestion_log ORDER BY created_at DESC LIMIT 5")).fetchall()
            if not rows:
                print("    (empty - the pipeline has never logged a run)")
            for r in rows:
                print(f"    {str(r[0])[:19]}  {str(r[1])[:14]:<14} "
                      f"{str(r[2])[:8]:<8} {str(r[3] or '')[:40]}")
        except Exception as exc:
            print(f"    ERR {str(exc)[:60]}")

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
    print(f"  Newest data is {hrs:.1f}h old ({age.days}d).")
    if age < timedelta(hours=6):
        print("  VERDICT: ALIVE - the 2-hourly cron is running.")
        return 0
    print("  VERDICT: STALE - nothing recent.")
    print("  -> GitHub > repo > Actions tab. A scheduled workflow is")
    print("     auto-disabled after 60 days of repo inactivity; re-enable it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
