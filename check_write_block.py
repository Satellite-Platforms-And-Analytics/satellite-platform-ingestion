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

from src.db.writer import get_engine                         # noqa: E402

SETTINGS = ("statement_timeout", "lock_timeout",
            "idle_in_transaction_session_timeout",
            "default_transaction_read_only")


def endpoint() -> None:
    url = os.environ.get("DATABASE_URL") or ""
    if not url:
        print("  DATABASE_URL is not set.")
        return
    u = urlsplit(url)
    kind = {6543: "TRANSACTION pooler", 5432: "session pooler or direct"}
    print(f"  configured endpoint: {u.hostname}:{u.port}  "
          f"user={u.username}")
    print(f"    {kind.get(u.port or 0, 'unrecognised port')}")
    if u.port == 6543:
        print("    6543 hands out a different backend per statement. "
              "check_db_endpoints.py\n    reported this port FAILING "
              "earlier today while 5432 answered.")


def main() -> int:
    print("\n  ENDPOINT")
    endpoint()

    with get_engine().connect() as conn:
        print("\n  WHAT IS IN cost_claims NOW (so 'nothing was written' is "
              "measured, not assumed)")
        if conn.execute(text(
                "SELECT to_regclass('public.cost_claims') IS NULL")
        ).scalar_one():
            print("    cost_claims does not exist. Apply 021 first.")
            return 1
        for src, n, lo, hi in conn.execute(text("""
                SELECT source, count(*), min(as_of_date), max(as_of_date)
                  FROM cost_claims GROUP BY source ORDER BY source""")):
            print(f"    {str(src):<16}{n:>7} rows   {lo} .. {hi}")
        total = conn.execute(text(
            "SELECT count(*) FROM cost_claims")).scalar_one()
        print(f"    {'TOTAL':<16}{total:>7} rows")

        print("\n  TIMEOUTS THIS SESSION HAS")
        for s in SETTINGS:
            try:
                v = conn.execute(text(f"SHOW {s}")).scalar_one()
            except Exception as exc:                         # noqa: BLE001
                v = f"unreadable: {str(exc).splitlines()[0][:40]}"
            print(f"    {s:<40}{v}")

        print("\n  TIMEOUTS ATTACHED TO ROLES (pg_db_role_setting)")
        try:
            rows = list(conn.execute(text("""
                SELECT coalesce(r.rolname, 'ALL ROLES') AS who,
                       coalesce(d.datname, 'ALL DATABASES') AS db,
                       s.setconfig
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
                    print(f"    {who:<22}{db:<12}{keep}")
        except Exception as exc:                             # noqa: BLE001
            print(f"    unreadable: {str(exc).splitlines()[0][:60]}")

        print("\n  OTHER BACKENDS (anything idle in a transaction holds its "
              "locks)")
        try:
            rows = list(conn.execute(text("""
                SELECT pid, state, wait_event_type, wait_event,
                       round(extract(epoch FROM now() - xact_start))
                           AS xact_age_s,
                       left(coalesce(query, ''), 48) AS q
                  FROM pg_stat_activity
                 WHERE datname = current_database()
                   AND pid <> pg_backend_pid()
                 ORDER BY xact_start NULLS LAST""")))
            if not rows:
                print("    none. Nothing else is connected to this "
                      "database.")
            for pid, state, wt, we, age, q in rows:
                flag = ("   <-- HOLDING LOCKS"
                        if state == "idle in transaction" else "")
                print(f"    pid {pid:<8}{str(state):<22}"
                      f"xact {str(age):>6}s  {str(wt or '-')}/"
                      f"{str(we or '-')}{flag}")
                if q.strip():
                    print(f"        {q.strip()}")
        except Exception as exc:                             # noqa: BLE001
            print(f"    unreadable: {str(exc).splitlines()[0][:60]}")

        print("\n  LOCKS ON cost_claims RIGHT NOW")
        try:
            rows = list(conn.execute(text("""
                SELECT l.pid, l.mode, l.granted
                  FROM pg_locks l
                 WHERE l.relation = 'public.cost_claims'::regclass
                   AND l.pid <> pg_backend_pid()""")))
            if not rows:
                print("    none.")
            for pid, mode, granted in rows:
                print(f"    pid {pid:<8}{mode:<28}"
                      f"{'granted' if granted else 'WAITING'}")
        except Exception as exc:                             # noqa: BLE001
            print(f"    unreadable: {str(exc).splitlines()[0][:60]}")

        print("\n  TRIGGERS AND CONSTRAINTS ON cost_claims")
        try:
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
                  f"({', '.join(f'{t}:{n}' for n, t in con[:8])}"
                  f"{' ...' if len(con) > 8 else ''})")
        except Exception as exc:                             # noqa: BLE001
            print(f"    unreadable: {str(exc).splitlines()[0][:60]}")

        print("\n  ONE REAL INSERT, 20s local timeout, ROLLED BACK")
        print("    This is the measurement that separates the three "
              "causes.")
        conn.rollback()
        t0 = time.monotonic()
        try:
            conn.execute(text("SET LOCAL statement_timeout = '20s'"))
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(text("""
                INSERT INTO cost_claims (
                    subject_kind, subject_id, subject_label, metric,
                    value, currency, as_of_date, published_date, method,
                    source, source_url)
                VALUES ('enterprise', 'CIK0000000000',
                        'check_write_block probe', 'revenue', 1, 'USD',
                        DATE '1900-01-01', DATE '1900-01-01',
                        'published', 'sec_edgar',
                        'https://example.invalid/probe')
                ON CONFLICT (subject_kind, subject_id, metric, source,
                             as_of_date) DO NOTHING"""))
            dt = time.monotonic() - t0
            print(f"    OK in {dt * 1000:.0f} ms")
            if dt < 1:
                print("    So the insert itself is fast. The earlier "
                      "failure was the TIMEOUT\n    this session runs "
                      "under, not the work -- see the settings above.")
            else:
                print("    Slow on its own. Read the triggers and "
                      "constraints above.")
        except Exception as exc:                             # noqa: BLE001
            dt = time.monotonic() - t0
            first = str(exc).splitlines()[0][:100]
            print(f"    FAILED after {dt * 1000:.0f} ms: {first}")
            if "lock" in first.lower():
                print("    A LOCK. The blocking pid is in the backend "
                      "list above; it has to end\n    before any write "
                      "can land.")
        finally:
            conn.rollback()
            print("    rolled back.")

    print("\n  Nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
