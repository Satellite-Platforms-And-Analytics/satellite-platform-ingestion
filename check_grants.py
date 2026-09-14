"""
Report exactly what the public roles can reach, by asking the database.

    python check_grants.py

WHY THIS EXISTS
===============
`tests/test_db_privileges.py` found on 2026-09-14 that `authenticated`
holds TRUNCATE, TRIGGER and REFERENCES on eleven relations. The cause is
plain once seen: `003_revoke_anon_write_grants.sql` revoked them **from
anon only**, and every table created afterwards (006, 007, 009) remembered
to include `authenticated` while the ten that predated 003 never got
back-filled.

The test reports the finding. This reports the shape — which is the
difference between knowing a number and being able to act on it.

THE PART THE GRANT TABLE DOES NOT TELL YOU
==========================================
`001_core_schema.sql` creates three VIEWS: active_satellites,
satellites_by_country and ingestion_status. Views were invisible to the
migration audit, which only scanned CREATE TABLE.

They matter more than ordinary tables here. A PostgreSQL view runs with
its **owner's** privileges unless it is declared `security_invoker = true`
— and that default means a view can return rows that row-level security
would have refused to the caller directly.

`ingestion_status` selects pipeline, status, **message**, records and
duration from `ingestion_log`. `ingestion_log` has RLS enabled and
deliberately **no policy**, which is deny-all. And `message` carries
exception text: `log_step(..., message=str(exc)[:500])`. A connection
error's text contains the database host; a SQLAlchemy error can contain
the failing statement.

So the question is not academic: if that view is owner-rights and anon can
select from it, the last fifty ingestion log entries are readable through
the public API despite RLS, exception messages included.

HOW THIS ANSWERS IT
===================
Not by reading the grant table and reasoning. It does `SET LOCAL ROLE` and
**actually tries the read**, inside a transaction that is rolled back. A
grant is a claim about what should happen; a query run as the role is what
does happen, and this project has been caught before by the gap between
the two.
"""
from __future__ import annotations

import sys

from src.env import bootstrap

bootstrap()

from sqlalchemy import text                                  # noqa: E402

from src.db.writer import get_engine                         # noqa: E402

PUBLIC_ROLES = ("anon", "authenticated")
ALLOWED = {"SELECT"}


def grants(conn) -> None:
    rows = list(conn.execute(text("""
        SELECT grantee, table_name, privilege_type
          FROM information_schema.role_table_grants
         WHERE table_schema = 'public' AND grantee = ANY(:r)
         ORDER BY grantee, table_name, privilege_type
    """), {"r": list(PUBLIC_ROLES)}))

    by_rel: dict = {}
    for g, t, p in rows:
        by_rel.setdefault((g, t), []).append(p.upper())

    print("\n  GRANTS HELD BY THE PUBLIC ROLES")
    print(f"  {'role':<16}{'relation':<26}privileges")
    print("  " + "-" * 74)
    excess = 0
    for (g, t), ps in sorted(by_rel.items()):
        bad = [p for p in ps if p not in ALLOWED]
        mark = "  <-- beyond SELECT" if bad else ""
        excess += len(bad)
        print(f"  {g:<16}{t:<26}{', '.join(sorted(ps))}{mark}")
    print(f"\n  {excess} privilege(s) beyond SELECT.")


def views(conn) -> None:
    rows = list(conn.execute(text("""
        SELECT c.relname,
               pg_get_userbyid(c.relowner) AS owner,
               COALESCE(
                   (SELECT o FROM unnest(c.reloptions) o
                     WHERE o LIKE 'security_invoker=%'),
                   'security_invoker=false (default)') AS invoker
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')
         ORDER BY c.relname
    """)))
    print("\n  VIEWS — and whose privileges they run with")
    print(f"  {'view':<26}{'owner':<18}security_invoker")
    print("  " + "-" * 74)
    for name, owner, inv in rows:
        flag = "" if "true" in inv else "   <-- runs as owner, can bypass RLS"
        print(f"  {name:<26}{owner:<18}{inv}{flag}")
    return [r[0] for r in rows]


def can_anon_actually_read(conn, relations) -> None:
    """The decisive evidence: try it as the role, then roll back."""
    print("\n  WHAT anon CAN ACTUALLY READ (SET ROLE, then rolled back)")
    print(f"  {'relation':<26}rows visible to anon")
    print("  " + "-" * 74)
    # SQLAlchemy 2.0 autobegins a transaction on the first execute(), so
    # conn.begin() raises "already initialized a Transaction". Roll back
    # first, let the next execute() open a fresh one, and roll that back
    # too. SET LOCAL needs a transaction to be local to, which the
    # autobegin provides. (Found by running this, 2026-09-14.)
    for rel in relations:
        conn.rollback()
        try:
            conn.execute(text("SET LOCAL ROLE anon"))
            n = conn.execute(text(f"SELECT count(*) FROM {rel}")).scalar_one()
            note = ""
            if rel in ("ingestion_status", "ingestion_log") and n:
                note = "   <-- RLS says deny-all; this is a bypass"
            print(f"  {rel:<26}{n}{note}")
        except Exception as exc:                             # noqa: BLE001
            print(f"  {rel:<26}refused: {str(exc).splitlines()[0][:52]}")
        finally:
            conn.rollback()


def main() -> int:
    engine = get_engine()
    with engine.connect() as conn:
        grants(conn)
        view_names = views(conn)
        can_anon_actually_read(
            conn, view_names + ["ingestion_log", "satellites", "tle_history"])
    print("\n  Read-only. Nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
