#!/usr/bin/env python
"""Why did a one-row INSERT into cost_claims time out?

    python check_write_block.py

`--apply` died on its FIRST insert with

    psycopg2.errors.QueryCanceled: canceling statement due to
    statement timeout

which is NOT the pooler error that preceded it. The connection worked
and the SERVER killed the statement, so the two have different causes
and this file exists to tell them apart rather than guess.

A one-row INSERT ... ON CONFLICT is not slow work. The candidates, in
the order this script tests them:

  1. SOMETHING ELSE HOLDS A LOCK. The likeliest story given the
     transaction pooler flapping all afternoon: an earlier connection
     died mid-transaction, the pooler kept the server-side backend, and
     it still sits `idle in transaction` holding a lock on cost_claims.
     Every INSERT then waits for it and dies on statement_timeout.
     `pg_stat_activity` and `pg_locks` say so plainly.

  2. statement_timeout IS SIMPLY VERY SHORT for this role, and a round
     trip through a sick pooler exceeds it. `SHOW` says what this
     session has; pg_db_role_setting says what the role carries.

  3. A TRIGGER OR CONSTRAINT makes the insert expensive. One row should
     not be, but a trigger doing per-row work on a large table would
     be, so the triggers and constraints are listed.

Then it tries ONE real insert inside a transaction it rolls back, with
statement_timeout raised LOCALLY to 20s, and times it. That single
measurement separates the three: it succeeds fast (so the earlier
failure was the timeout being short), it waits and then reports a
blocking pid (a lock), or it takes real seconds on its own (work).

NEVER PRINTS A PASSWORD. It prints host, port, user, settings and
timings. Nothing is left changed: the insert is rolled back.
"""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlsplit

from src.env import bootstrap

bootstrap()

from sqlalchemy import text                                  # noqa: E402

from src.db.writer import brief_db_error, get_engine         # noqa: E402

SETTINGS = ("statement_timeout", "lock_timeout",
            "idle_in_transaction_session_timeout",
            "default_transaction_read_only", "transaction_read_only")

#: Every section runs under its OWN generous timeout, so a short global
#: statement_timeout cannot kill the tool that exists to report it.
SECTION_TIMEOUT = "15s"
SECTION_LOCK_TIMEOUT = "5s"


def endpoint() -> "int | None":
    """-> the configured port, or None. Prints no credential."""
    url = os.environ.get("DATABASE_URL") or ""
    if not url:
        print("  DATABASE_URL is not set.")
        return None
    u = urlsplit(url)
    kind = {6543: "TRANSACTION pooler", 5432: "session pooler or direct"}
    print(f"  configured endpoint: {u.hostname}:{u.port}  "
          f"user={u.username}")
    print(f"    {kind.get(u.port or 0, 'unrecognised port')}")
    if u.port == 6543:
        print("    6543 hands out a different backend per statement. "
              "check_db_endpoints.py\n    reported this port FAILING "
              "earlier today while 5432 answered.")
    return u.port


def section(conn, title: str, fn) -> None:
    """Run one question, timed, under its own timeout, and never let it
    take the rest of the diagnosis with it.

    ORDERING AND ISOLATION ARE THE WHOLE DESIGN HERE, and the first
    version of this file had neither. It put the table reads BEFORE the
    settings and the locks, in one unguarded block, so when
    `SELECT count(*)` on a 25-row table timed out the run died with
    every cheap question still unasked -- the timeouts, the blocking
    pids, the locks. A diagnostic has to be ordered by what survives
    failure, not by what reads well, and each question has to fail
    alone.
    """
    print(f"\n  {title}")
    t0 = time.monotonic()
    try:
        conn.rollback()
        conn.execute(text(f"SET LOCAL statement_timeout = "
                          f"'{SECTION_TIMEOUT}'"))
        conn.execute(text(f"SET LOCAL lock_timeout = "
                          f"'{SECTION_LOCK_TIMEOUT}'"))
        fn(conn)
        print(f"    [{(time.monotonic() - t0) * 1000:.0f} ms]")
    except Exception as exc:                                 # noqa: BLE001
        print(f"    FAILED after {(time.monotonic() - t0) * 1000:.0f} ms: "
              f"{brief_db_error(exc)}")
    finally:
        try:
            conn.rollback()
        except Exception:                                    # noqa: BLE001
            pass


def _latency(conn) -> None:
    for i in range(3):
        t0 = time.monotonic()
        conn.execute(text("SELECT 1"))
        ms = (time.monotonic() - t0) * 1000
        print(f"    SELECT 1 #{i + 1}: {ms:.0f} ms")
    print("    If these are slow or uneven, the link is the problem and "
          "nothing below\n    is about cost_claims at all.")


def _settings(conn) -> None:
    for s in SETTINGS:
        try:
            v = conn.execute(text(f"SHOW {s}")).scalar_one()
        except Exception as exc:                             # noqa: BLE001
            v = f"unreadable: {brief_db_error(exc)[:40]}"
        print(f"    {s:<40}{v}")
    print("    A `statement_timeout` of a few seconds is the simplest "
          "explanation for a\n    one-row insert being cancelled. This "
          "is the line that settles it.")


def _role_settings(conn) -> None:
    rows = list(conn.execute(text("""
        SELECT coalesce(r.rolname, 'ALL ROLES') AS who,
               coalesce(d.datname, 'ALL DATABASES') AS db, s.setconfig
          FROM pg_db_role_setting s
          LEFT JOIN pg_roles r ON r.oid = s.setrole
          LEFT JOIN pg_database d ON d.oid = s.setdatabase
         ORDER BY who""")))
    if not rows:
        print("    none set per role or per database.")
    for who, db, cfg in rows:
        keep = [c for c in (cfg or [])
                if "timeout" in c or "read_only" in c]
        if keep:
            print(f"    {who:<22}{db:<14}{keep}")


def _activity(conn) -> None:
    rows = list(conn.execute(text("""
        SELECT pid, state, wait_event_type, wait_event,
               round(extract(epoch FROM now() - xact_start)) AS xact_age_s,
               round(extract(epoch FROM now() - backend_start)) AS conn_age_s,
               pg_blocking_pids(pid) AS blockers,
               left(coalesce(query, ''), 44) AS q
          FROM pg_stat_activity
         WHERE datname = current_database() AND pid <> pg_backend_pid()
         ORDER BY xact_start NULLS LAST""")))
    if not rows:
        print("    none. Nothing else is connected to this database.")
    for pid, st, wt, we, xage, cage, blockers, q in rows:
        flag = "   <-- HOLDS ITS LOCKS" if st == "idle in transaction" else ""
        print(f"    pid {pid:<8}{str(st):<22}xact {str(xage):>6}s  "
              f"conn {str(cage):>6}s  {str(wt or '-')}/{str(we or '-')}{flag}")
        if blockers:
            print(f"        BLOCKED BY {list(blockers)}")
        if q.strip():
            print(f"        {q.strip()}")


def _locks(conn) -> None:
    rows = list(conn.execute(text("""
        SELECT l.pid, coalesce(c.relname, '-') AS rel, l.mode, l.granted
          FROM pg_locks l
          LEFT JOIN pg_class c ON c.oid = l.relation
         WHERE l.pid <> pg_backend_pid() AND l.locktype = 'relation'
         ORDER BY l.granted, rel""")))
    if not rows:
        print("    no relation locks held by any other backend.")
    for pid, rel, mode, granted in rows:
        mark = "granted" if granted else "WAITING"
        flag = ("   <-- blocks even a SELECT"
                if "AccessExclusive" in str(mode) else "")
        print(f"    pid {pid:<8}{str(rel)[:28]:<30}{str(mode):<26}"
              f"{mark}{flag}")


def _shape(conn) -> None:
    trg = list(conn.execute(text("""
        SELECT tgname FROM pg_trigger
         WHERE tgrelid = 'public.cost_claims'::regclass
           AND NOT tgisinternal""")))
    print(f"    triggers: {[t[0] for t in trg] or 'none'}")
    con = list(conn.execute(text("""
        SELECT conname, contype FROM pg_constraint
         WHERE conrelid = 'public.cost_claims'::regclass
         ORDER BY contype, conname""")))
    print(f"    constraints: {len(con)} "
          f"({', '.join(f'{t}:{n}' for n, t in con[:6])}"
          f"{' ...' if len(con) > 6 else ''})")
    rel = conn.execute(text("""
        SELECT pg_size_pretty(pg_total_relation_size(
                   'public.cost_claims'::regclass)),
               (SELECT reltuples::bigint FROM pg_class
                 WHERE oid = 'public.cost_claims'::regclass)""")).one()
    print(f"    size on disk: {rel[0]}   planner's row estimate: {rel[1]}")


def _contents(conn) -> None:
    for src, n, lo, hi in conn.execute(text("""
            SELECT source, count(*), min(as_of_date), max(as_of_date)
              FROM cost_claims GROUP BY source ORDER BY source""")):
        print(f"    {str(src):<16}{n:>7} rows   {lo} .. {hi}")
    total = conn.execute(text(
        "SELECT count(*) FROM cost_claims")).scalar_one()
    print(f"    {'TOTAL':<16}{total:>7} rows")
    print("    Measured, so 'nothing was written' is a measurement.")


def _probe_insert(conn) -> None:
    conn.execute(text("""
        INSERT INTO cost_claims (
            subject_kind, subject_id, subject_label, metric, value,
            currency, as_of_date, published_date, method, source,
            source_url)
        VALUES ('enterprise', 'CIK0000000000',
                'check_write_block probe', 'revenue', 1, 'USD',
                DATE '1900-01-01', DATE '1900-01-01', 'published',
                'sec_edgar', 'https://example.invalid/probe')
        ON CONFLICT (subject_kind, subject_id, metric, source, as_of_date)
        DO NOTHING"""))
    print("    the insert itself completed. Rolling back.")


def main() -> int:
    print("\n  ENDPOINT")
    port = endpoint()

    # THIS SCRIPT COMMITTED THE FAILURE IT WAS WRITTEN TO REPORT, TWICE.
    # First it went straight into get_engine().connect() with no wrap and
    # produced a 60-line traceback -- AD-123's lesson, one file later.
    # Then, wrapped, it asked its questions in the wrong ORDER and in one
    # unguarded block, so a timeout on `SELECT count(*)` over 25 rows
    # killed the run with every cheap question unasked. Hence section().
    try:
        conn_ctx = get_engine().connect()
    except Exception as exc:                                 # noqa: BLE001
        print("\n  COULD NOT CONNECT, so none of the questions below "
              "could be asked.")
        print(f"    driver: {brief_db_error(exc)}")
        if port == 6543:
            print("    AND THE CONFIGURED PORT IS THE ONE THAT HAS "
                  "FAILED EVERY PROBE TODAY.\n    Nothing here can be "
                  "measured until DATABASE_URL points at 5432. That is "
                  "the\n    whole finding: change the port, then re-run "
                  "this.")
        else:
            print("    Run `python check_db_endpoints.py`: it tries both "
                  "pooler ports and the\n    direct host with the same "
                  "credential and reports which complete a handshake.")
        return 1

    with conn_ctx as conn:
        if conn.execute(text(
                "SELECT to_regclass('public.cost_claims') IS NULL")
        ).scalar_one():
            print("\n  cost_claims does not exist. Apply 021 first.")
            return 1
        # Cheapest and most decisive first, table reads last.
        section(conn, "ROUND-TRIP LATENCY", _latency)
        section(conn, "TIMEOUTS THIS SESSION RUNS UNDER", _settings)
        section(conn, "TIMEOUTS ATTACHED TO ROLES", _role_settings)
        section(conn, "OTHER BACKENDS, AND WHO BLOCKS WHOM", _activity)
        section(conn, "RELATION LOCKS HELD BY ANYONE ELSE", _locks)
        section(conn, "SHAPE OF cost_claims", _shape)
        section(conn, "WHAT cost_claims HOLDS NOW", _contents)
        section(conn, "ONE REAL INSERT, ROLLED BACK", _probe_insert)

    print("\n  Nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
