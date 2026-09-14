"""
The database's access control is asserted, not remembered.

WHY THIS EXISTS
===============
Row-level security and the anon grants were got right, carefully, across
001, 002, 003 and 009 — and then verified exactly once, by a person
reading the output of a migration. Nothing since has been able to notice a
change. That is the shape this project keeps finding and then rebuilding
the same way: a health check watching three pipelines out of five, a
config test checking the leaves and not the root, a monitor that could not
be silent. **A control that nobody can fail is not a control.**

Concretely, all of the following would pass every existing test:

- a new table created without `ENABLE ROW LEVEL SECURITY`, which on
  Supabase is born with `anon` holding privileges from the project's
  default grants
- `GRANT INSERT ON satellites TO anon` applied by hand during debugging
  and never revoked
- RLS disabled on one table to chase a bug on a Sunday
- a policy widened from `SELECT` to `ALL`

None of those break an ingestion run, a CI job, or the globe. They are
invisible in every artefact the project produces — until they are not.

WHAT IT ASSERTS
===============
1. Every table in `public` has row-level security **enabled**.
2. `anon` and `authenticated` hold **no privilege other than SELECT** on
   any table. Not INSERT, not UPDATE, not DELETE, and specifically not
   TRUNCATE — 003 exists because RLS does not apply to TRUNCATE, so a
   correct policy offers no protection at all against a role that can
   empty the table outright.
3. Every table readable by `anon` has at least one policy. RLS with no
   policy is deny-all, which is safe but silent: it is how a table can
   look published and return zero rows forever.
4. Every view runs with `security_invoker = true`.

Assertion 4 was added 2026-09-14, after assertion 2 failed on its first
live run and the investigation found three views that no audit in this
project had ever looked at. Every privilege scan written here had matched
`CREATE TABLE`, and assertion 1 filters to `relkind = 'r'`, so
`active_satellites`, `satellites_by_country` and `ingestion_status` were
invisible to all of it.

A view runs with its **owner's** privileges unless declared
`security_invoker = true`, which means an owner-rights view hands back
rows that row-level security would have refused the caller. That is not
hypothetical for `ingestion_status`: it selects `message` out of
`ingestion_log`, a deny-all table, and `message` holds exception text that
can contain the database host or a failing statement.

SKIPPING IS NOT PASSING
=======================
No DATABASE_URL means skip, because most runs of this suite are offline.
But a guard that skips by design must fail somewhere by default, or
"it runs on the workstation" is a belief rather than a fact (AD-045, and
the four WIT tests that had skipped in CI since they were written). Set
**REQUIRE_DB=1** and a missing DATABASE_URL is a failure. That is what the
workstation and any future scheduled security check should set.
"""
from __future__ import annotations

import os

import pytest

# Load .env before reading DATABASE_URL.
#
# THIS LINE IS A BUG FIX, AND THE BUG WAS ALREADY DOCUMENTED
# ==========================================================
# Without it, `pytest tests/test_db_privileges.py` reported "DATABASE_URL
# is not set" on a workstation where DATABASE_URL is plainly sitting in
# .env — while `pytest` over the whole suite worked. The difference is
# import order: test_satcat.py imports src/catalog/seed_satcat.py, which
# calls load_env() at import time, so by the time this module ran the
# environment had been populated as a side effect of an unrelated test.
#
# A security control whose result depends on which tests ran before it is
# not a control, and "DATABASE_URL is not set" when it is set is the worst
# possible message: it sends you to look at the wrong thing.
#
# src/env.py exists precisely for this. Its docstring describes this exact
# failure, observed 2026-09-04. It was written, and then this entry point
# was added on 2026-09-13 without calling it.
#
# The project's own rule from 09-12 — read the file in the repo about the
# thing before writing the thing — applies to its own modules, not only to
# data sources.
from src.env import bootstrap

bootstrap()

REQUIRE_DB = bool(os.environ.get("REQUIRE_DB"))

#: The roles PostgREST assumes on behalf of the public internet. Anything
#: they hold is, effectively, held by anyone who can reach the API.
PUBLIC_ROLES = ("anon", "authenticated")

#: The only privilege either role has any business holding here. The
#: platform is read-only to the public by design; every write goes through
#: the ingestion pipeline with its own credentials.
ALLOWED = {"SELECT"}


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        msg = ("DATABASE_URL is not set in the environment and was not "
               "found in .env, so the live privilege state cannot be "
               "checked")
        if REQUIRE_DB:
            pytest.fail(msg + " — REQUIRE_DB is set.")
        pytest.skip(msg)
    try:
        from src.db.writer import get_engine
    except ImportError as exc:                              # pragma: no cover
        pytest.skip(f"writer unavailable: {exc}")
    return get_engine()


@pytest.fixture(scope="module")
def conn():
    """
    A connection, or a skip — but never a noisy error.

    A DATABASE_URL that is set and unreachable is the normal state in an
    offline environment (CI without the secret, a sandbox with no egress).
    Erroring there would make three red entries in every such run, and a
    suite that is routinely red is a suite nobody reads. Under REQUIRE_DB
    it is a failure, because then the caller has asserted the database
    should be reachable.
    """
    engine = _connect()

    # The failure is captured and acted on *outside* the except block, so
    # the reported result is the sentence rather than a psycopg2 traceback
    # with the sentence buried under it. A security check that is hard to
    # read is a security check that gets skimmed.
    c = None
    problem = None
    try:
        c = engine.connect()
    except Exception as exc:                                # noqa: BLE001
        problem = str(exc).splitlines()[0][:160]

    if problem is not None:
        msg = f"DATABASE_URL is set but unreachable: {problem}"
        if REQUIRE_DB:
            pytest.fail(msg + " — REQUIRE_DB is set.")
        pytest.skip(msg)

    try:
        yield c
    finally:
        c.close()


def test_row_level_security_is_enabled_on_every_table(conn):
    from sqlalchemy import text
    rows = list(conn.execute(text("""
        SELECT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND NOT c.relrowsecurity
         ORDER BY c.relname
    """)))
    naked = [r[0] for r in rows]
    assert not naked, (
        "row-level security is OFF for: " + ", ".join(naked) + ".\n"
        "On Supabase a table without RLS is readable and writable by "
        "anon to whatever extent the role's grants allow, and the "
        "project's default grants are generous. Add "
        "`ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;` plus a policy to "
        "the migration that created it."
    )


def test_public_roles_hold_nothing_beyond_select(conn):
    from sqlalchemy import text
    rows = list(conn.execute(text("""
        SELECT grantee, table_name, privilege_type
          FROM information_schema.role_table_grants
         WHERE table_schema = 'public'
           AND grantee = ANY(:roles)
         ORDER BY grantee, table_name, privilege_type
    """), {"roles": list(PUBLIC_ROLES)}))

    excess = [(g, t, p) for g, t, p in rows if p.upper() not in ALLOWED]
    assert not excess, (
        "public roles hold privileges beyond SELECT:\n  "
        + "\n  ".join(f"{g} has {p} on {t}" for g, t, p in excess)
        + "\n\nTRUNCATE is the one that bites hardest: row-level security "
          "does not apply to it, so every 'Public read' policy can be "
          "correct and the table can still be emptied. See "
          "003_revoke_anon_write_grants.sql."
    )


def test_readable_tables_have_a_policy(conn):
    """RLS with no policy is deny-all — safe, and silent."""
    from sqlalchemy import text
    rows = list(conn.execute(text("""
        SELECT DISTINCT g.table_name
          FROM information_schema.role_table_grants g
         WHERE g.table_schema = 'public'
           AND g.grantee = 'anon'
           AND g.privilege_type = 'SELECT'
           AND NOT EXISTS (
               SELECT 1 FROM pg_policies p
                WHERE p.schemaname = 'public'
                  AND p.tablename = g.table_name)
         ORDER BY 1
    """)))
    silent = [r[0] for r in rows]
    assert not silent, (
        "granted SELECT to anon but no policy exists, so the table "
        "returns zero rows to every public reader: " + ", ".join(silent)
        + ".\nEither add the policy or drop the grant — a grant that "
          "cannot be exercised is a claim the schema does not honour."
    )


def test_views_run_with_the_callers_privileges(conn):
    """
    An owner-rights view is a hole with a polite name.

    `security_invoker = false` is PostgreSQL's default, so this is not a
    mistake anyone made - it is what happens when nobody states otherwise.
    The consequence is that RLS on the underlying tables does not protect
    the view's output, and the only thing standing between a deny-all
    table and a public reader is the absence of a GRANT on the view.

    That absence is one convenient `GRANT SELECT` away from being gone,
    and the request that produces it - "can we show ingestion status on a
    page?" - is entirely reasonable. This test is what makes that grant
    safe instead of silently catastrophic.

    Fixed by 010_revoke_authenticated_and_seal_views.sql; this fails until
    that migration is applied, which is the point of it.
    """
    from sqlalchemy import text
    rows = list(conn.execute(text("""
        SELECT c.relname,
               COALESCE(
                   (SELECT o FROM unnest(c.reloptions) o
                     WHERE o LIKE 'security_invoker=%'),
                   'unset (defaults to false)') AS invoker
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')
         ORDER BY c.relname
    """)))
    owner_rights = [name for name, inv in rows if "true" not in inv.lower()]
    assert not owner_rights, (
        "these views run with their owner's privileges, so row-level "
        "security on the tables beneath them does not apply to what they "
        "return: " + ", ".join(owner_rights)
        + "\n\nAdd `ALTER VIEW <name> SET (security_invoker = true);` to the "
          "migration that creates the view. See "
          "010_revoke_authenticated_and_seal_views.sql."
    )
