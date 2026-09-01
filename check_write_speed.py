"""
Which pipeline steps actually completed, and how long did they take?

    python check_write_speed.py

The fetcher does not populate duration_s, so elapsed time is derived from
the gap between consecutive steps of the same run instead.
"""
import os
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import create_engine, text

e = create_engine(os.environ["DATABASE_URL"])
with e.connect() as c:
    print("\n  Recent ingestion_log entries (all steps, newest first)")
    print("  " + "-" * 66)
    print(f"  {'created_at':<20}{'pipeline':<11}{'step':<11}{'status':<9}{'records':>8}{'dur_s':>8}")
    rows = c.execute(text(
        "SELECT created_at, pipeline, step, status, records_processed, duration_s "
        "FROM ingestion_log ORDER BY created_at DESC LIMIT 20")).fetchall()
    for r in rows:
        dur = f"{r[5]:.1f}" if r[5] is not None else "-"
        print(f"  {str(r[0])[:19]:<20}{str(r[1])[:10]:<11}{str(r[2])[:10]:<11}"
              f"{str(r[3])[:8]:<9}{(r[4] or 0):>8,}{dur:>8}")

    print("\n  Runs that fetched but never wrote")
    print("  " + "-" * 66)
    orphans = c.execute(text("""
        SELECT f.created_at, f.records_processed
        FROM ingestion_log f
        WHERE f.pipeline = 'tle_fetch' AND f.step = 'fetch' AND f.status = 'success'
          AND NOT EXISTS (
            SELECT 1 FROM ingestion_log w
            WHERE w.pipeline = 'tle_fetch' AND w.step = 'write_db'
              AND w.created_at BETWEEN f.created_at AND f.created_at + interval '30 minutes')
        ORDER BY f.created_at DESC LIMIT 10""")).fetchall()
    if not orphans:
        print("    none - every fetch was followed by a write")
    for o in orphans:
        print(f"    {str(o[0])[:19]}   fetched {o[1]:,} records, no write_db logged")

    print("\n  Fetch -> write elapsed, where both were logged")
    print("  " + "-" * 66)
    pairs = c.execute(text("""
        SELECT f.created_at, w.created_at,
               EXTRACT(EPOCH FROM (w.created_at - f.created_at)) AS secs,
               w.records_processed
        FROM ingestion_log f
        JOIN ingestion_log w
          ON w.pipeline = 'tle_fetch' AND w.step = 'write_db'
         AND w.created_at BETWEEN f.created_at AND f.created_at + interval '30 minutes'
        WHERE f.pipeline = 'tle_fetch' AND f.step = 'fetch'
        ORDER BY f.created_at DESC LIMIT 8""")).fetchall()
    for p in pairs:
        rate = (p[3] / p[2]) if p[2] else 0
        print(f"    {str(p[0])[:19]}  ->  {p[2]:6.0f}s for {p[3]:,} rows  ({rate:.0f}/sec)")
