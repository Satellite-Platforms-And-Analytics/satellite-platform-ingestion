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
    def q(sql, params=None):
        try:
            with engine.connect() as c:
                return c.execute(text(sql), params or {}).fetchall()
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

    # ── per-pipeline liveness ────────────────────────────────────────────────
    #
    # Row counts answer "is there data". They do not answer "is each job
    # still running", which is the question that went unanswered from
    # 2026-08-26 to 08-31 while ingest_tle.yml logged `fetch success` and
    # wrote nothing. A job that stops is silent; the only evidence is a
    # step that should have appeared and did not.
    #
    # Each pipeline is checked against its own cadence rather than one
    # global freshness rule, with a generous multiple: scheduled workflows
    # on shared runners are best-effort and were observed landing 8-12h
    # apart on a 2-hourly cron.
    EXPECTED = {                      # pipeline -> (cadence_h, stale_after_h)
        "tle_fetch":   (2,  14),
        "propagation": (1,  14),
        "visibility":  (24, 48),
    }

    print("\n  Pipeline liveness")
    print("  " + "-" * 52)
    print(f"    {'pipeline':<14}{'last success':<21}{'age':>8}   state")

    stale_pipelines = []
    for pipeline, (cadence_h, stale_h) in EXPECTED.items():
        r = q("SELECT max(created_at) FROM ingestion_log "
              "WHERE pipeline = :p AND status IN ('success', 'partial')",
              {"p": pipeline})
        last = None if isinstance(r, tuple) or not r else r[0][0]

        if last is None:
            print(f"    {pipeline:<14}{'never':<21}{'-':>8}   NO RUNS LOGGED")
            stale_pipelines.append(pipeline)
            continue

        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        state = "ok" if age_h <= stale_h else f"STALE (expected every {cadence_h}h)"
        if age_h > stale_h:
            stale_pipelines.append(pipeline)
        print(f"    {pipeline:<14}{str(last)[:19]:<21}{age_h:>7.1f}h   {state}")

    if stale_pipelines:
        print(f"\n    {len(stale_pipelines)} pipeline(s) not reporting: "
              f"{', '.join(stale_pipelines)}")
        print("    Check the Actions tab - a workflow killed by its timeout")
        print("    logs nothing at all, so absence here is the only signal.")

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

    # Thresholds reflect what GitHub Actions actually delivers, not what the
    # cron asks for. Scheduled workflows on the free tier are best-effort and
    # are routinely delayed or dropped under load: a `0 */2 * * *` schedule
    # was observed landing 8-12h apart on 2026-08-31. Treating a 3h gap as a
    # failure would cry wolf on every run.
    if age < timedelta(hours=14):
        print("  VERDICT: ALIVE - runs are landing.")
        if hrs > 4:
            print(f"  (Cron asks for every 2h; last gap was {hrs:.0f}h. GitHub")
            print("   throttles scheduled workflows - expected, not a fault.)")
        return 0
    if age < timedelta(hours=48):
        print(f"  VERDICT: LAGGING - {hrs:.0f}h since the last run.")
        print("  Longer than GitHub's usual throttling. Check the Actions tab")
        print("  for failures before assuming it is fine.")
        return 1
    print(f"  VERDICT: STALE - nothing for {age.days}d.")
    print("  -> GitHub > repo > Actions tab. Check for failed runs, and note")
    print("     that a scheduled workflow is auto-disabled after 60 days of")
    print("     repository inactivity.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
