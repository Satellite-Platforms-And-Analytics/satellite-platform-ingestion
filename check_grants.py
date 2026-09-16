"""
Report exactly what a role can reach, by asking the database.

    python check_grants.py                  # the public roles: anon, authenticated
    python check_grants.py --role pipeline  # a least-privilege role, pass/fail

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


# ── The pipeline role (AD-065) ───────────────────────────────────────
#
# WHY THIS IS A SEPARATE MODE AND NOT A PARAGRAPH IN 019.
#
# 019 creates the role and ends with a block of SQL a person could run
# to check it. That is a note, and rule 7 of the security baseline says a
# finding gets a check that can go red. This is the check: it exits
# non-zero when the role's actual reach differs from what it is supposed
# to be, so a drift is a failure somewhere rather than something nobody
# thought to re-run.
#
# THE EXPECTED STATE IS WRITTEN HERE, NOT READ FROM THE MIGRATION.
# A checker that parsed 019 would agree with 019 by construction and
# could never catch a hand-made GRANT in the SQL editor, which is
# exactly how privilege creeps back. Two independent statements of the
# same intent; a disagreement between them is itself the finding.

#: SELECT, INSERT, UPDATE - and nothing else.
PIPELINE_WRITE = (
    "satellites", "satellite_attribution", "orbital_positions",
    "visibility_windows", "tle_history", "catalog_events",
    "ingestion_log", "archive_watermark", "imagery_scenes", "sensors",
    "organizations", "research_organizations", "research_projects",
    "research_project_taxonomy", "research_taxonomy_areas",
)

#: SELECT only. Their source of truth is a seed file in the repository,
#: so no automation has any business writing them.
PIPELINE_READ_ONLY = (
    "countries", "domains", "technologies", "technology_categories",
)

#: Never, on anything.
FORBIDDEN = ("DELETE", "TRUNCATE")


def probe_role(conn, role: str) -> int:
    """
    -> number of violations. Non-zero is a failing control.

    Three layers, because any one of them alone can be satisfied while
    the role is still wrong:

      1. has_table_privilege - the EFFECTIVE privilege, which is not the
         same as the grant table. A self-GRANT, for instance, returns a
         WARNING rather than an error, so a check reading psql's exit
         code would call it a success and grant nothing. Ask what the
         role can do, never what a statement returned.
      2. a write POLICY per writable table. Every table here has RLS
         enabled, and a GRANT without a policy leaves INSERTs failing and
         UPDATEs matching zero rows - and an UPDATE that matches nothing
         is not an error. The pipeline would report success while
         writing nothing.
      3. an actual UPDATE, as the role, rolled back. Layers 1 and 2 can
         both look right and still not compose. One row, chosen by ctid,
         so this stays cheap on a 200,000-row table.
    """
    bad = 0

    def priv(table: str, p: str) -> bool:
        return bool(conn.execute(
            text("SELECT has_table_privilege(:r, :t, :p)"),
            {"r": role, "t": table, "p": p}).scalar())

    exists = {r[0] for r in conn.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}

    print(f"\n  ROLE `{role}` — EFFECTIVE PRIVILEGES")
    print(f"  {'relation':<30}{'SELECT':<9}{'INSERT':<9}{'UPDATE':<9}"
          f"{'DELETE':<9}TRUNCATE")
    print("  " + "-" * 74)

    for t in PIPELINE_WRITE + PIPELINE_READ_ONLY:
        if t not in exists:
            print(f"  {t:<30}absent from this database")
            continue
        want_write = t in PIPELINE_WRITE
        got = {p: priv(t, p) for p in
               ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")}
        problems = []
        if not got["SELECT"]:
            problems.append("no SELECT")
        for p in ("INSERT", "UPDATE"):
            if want_write and not got[p]:
                problems.append(f"no {p}")
            if not want_write and got[p]:
                problems.append(f"{p} on a read-only table")
        for p in FORBIDDEN:
            if got[p]:
                problems.append(p)
        bad += len(problems)
        cells = "".join(f"{('yes' if got[p] else '-'):<9}" for p in
                        ("SELECT", "INSERT", "UPDATE", "DELETE"))
        cells += "yes" if got["TRUNCATE"] else "-"
        mark = f"   <-- {', '.join(problems)}" if problems else ""
        print(f"  {t:<30}{cells}{mark}")

    # ── Layer 2: a policy, or the grant is decorative ────────────────
    policies = {r[0] for r in conn.execute(text("""
        SELECT tablename FROM pg_policies
         WHERE schemaname = 'public'
           AND :r = ANY(roles)
           AND cmd IN ('ALL', 'INSERT', 'UPDATE')
    """), {"r": role})}
    print(f"\n  RLS WRITE POLICIES NAMING `{role}`")
    missing = [t for t in PIPELINE_WRITE
               if t in exists and t not in policies]
    if missing:
        bad += len(missing)
        for t in missing:
            print(f"  {t:<30}<-- GRANTed but no policy: writes will be "
                  f"silently refused")
    else:
        print(f"  all {len(policies & set(PIPELINE_WRITE))} writable "
              f"tables carry one")

    # ── Layer 3: try it ──────────────────────────────────────────────
    print(f"\n  AN ACTUAL WRITE, AS `{role}`, ROLLED BACK")
    probe = next((t for t in ("ingestion_log", "satellites") if t in exists),
                 None)
    if probe is None:
        print("  no probe table present; skipped")
    else:
        conn.rollback()
        try:
            # NOT just is_updatable. An identity column reports
            # is_updatable='YES' and then refuses assignment with
            # "column can only be updated to DEFAULT" - which the first
            # run of this probe hit on `id`, reporting a violation that
            # was the probe's fault rather than the role's. A control
            # that cries wolf is one people learn to ignore.
            col = conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema='public' AND table_name=:t
                   AND is_updatable='YES'
                   AND is_identity='NO'
                   AND is_generated='NEVER'
                 ORDER BY ordinal_position LIMIT 1
            """), {"t": probe}).scalar()
            if col is None:
                print(f"  {probe}: no plainly-updatable column; skipped")
                raise StopIteration
            conn.execute(text(f"SET LOCAL ROLE {role}"))
            n = conn.execute(text(
                f"WITH u AS (UPDATE {probe} SET {col} = {col} "
                f"WHERE ctid IN (SELECT ctid FROM {probe} LIMIT 1) "
                f"RETURNING 1) SELECT count(*) FROM u")).scalar_one()
            total = conn.execute(text(
                f"SELECT count(*) FROM {probe}")).scalar_one()
            if total == 0:
                print(f"  {probe}: empty, so the probe proves nothing")
            elif n == 1:
                print(f"  {probe}: UPDATE affected 1 row — grant and "
                      f"policy compose")
            else:
                bad += 1
                print(f"  {probe}: UPDATE affected {n} rows on a table "
                      f"holding {total} <-- RLS is swallowing writes")
        except StopIteration:
            pass
        except Exception as exc:                             # noqa: BLE001
            bad += 1
            print(f"  {probe}: refused — "
                  f"{str(exc).splitlines()[0][:60]}")
        finally:
            conn.rollback()

    print(f"\n  {bad} violation(s).")
    if bad:
        print("  This role's reach is not what it is supposed to be. "
              "See docs/PIPELINE_ROLE.md.")
    return bad


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", help="Probe this role against the expected "
                                   "least-privilege state and exit "
                                   "non-zero on any difference.")
    args = ap.parse_args(argv)

    engine = get_engine()
    with engine.connect() as conn:
        if args.role:
            exists = conn.execute(text(
                "SELECT 1 FROM pg_roles WHERE rolname = :r"),
                {"r": args.role}).scalar()
            if not exists:
                print(f"  role `{args.role}` does not exist. "
                      f"019_pipeline_role.sql creates it.")
                return 2
            attrs = conn.execute(text("""
                SELECT rolcanlogin, rolsuper, rolcreaterole, rolbypassrls
                  FROM pg_roles WHERE rolname = :r
            """), {"r": args.role}).one()
            print(f"\n  ROLE `{args.role}` — ATTRIBUTES")
            print(f"    login={attrs[0]}  superuser={attrs[1]}  "
                  f"createrole={attrs[2]}  bypassrls={attrs[3]}")
            extra = sum(1 for a in attrs[1:] if a)
            if extra:
                print("    <-- a least-privilege role holds none of "
                      "superuser, createrole or bypassrls")
            rc = probe_role(conn, args.role) + extra
            print("\n  Read-only. Nothing was changed.")
            return 1 if rc else 0

        grants(conn)
        view_names = views(conn)
        can_anon_actually_read(
            conn, view_names + ["ingestion_log", "satellites", "tle_history"])
    print("\n  Read-only. Nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
