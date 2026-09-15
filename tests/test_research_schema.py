"""
`015_research_activity.sql`'s constraints must be live in the database,
not merely written in the file.

WHY THIS IS A TEST AND NOT A COMMENT
====================================
015 carries four controls that exist to stop a specific defect, and each
one is silent when it works:

  - `tx_code` must match `^TX[0-9]{2}(\\.[0-9]+)*$`. This is AD-086 made
    enforceable. The failure it prevents is a parser writing a node's
    TITLE into the code column - which is exactly what the first TechPort
    survey did by keeping `title` and discarding `code` - after which a
    taxonomy column mixes `TX08` with `TX08.1.5` and groups **silently
    and wrongly**. Both are strings; only the pattern tells them apart.
  - `tx_top` is GENERATED, so an importer cannot supply a value that
    disagrees with the code it came from.
  - `organization_code` and `match_method` must both be present or both
    absent. A code with no method means the loosening that produced it
    became invisible (AD-085 keeps the relaxed match distinguishable).
  - `trl_current` between 1 and 9, or NULL. NULL is ordinary: 24% of
    TechPort projects state no TRL and they still belong in the table.

These were verified against PostgreSQL 16 before the migration was
committed - 16 assertions, every control refused what it should. But a
migration that passes on a scratch database and was never applied to
Supabase is a file, not a schema. This is what says it reached the real
one.

Skips when DATABASE_URL is absent or unreachable, fails under REQUIRE_DB,
and skips (rather than failing) when 015 has simply not been applied yet
- so it does not go red on every workstation between writing a migration
and running it.

TO RUN IT AGAINST THE LIVE DATABASE
===================================
This workstation is PowerShell, where `VAR=1 cmd` is not a thing - it is
read as a command named `VAR=1`, which is the error it gives. Both forms,
because the bash one keeps getting written from habit:

    PowerShell:   $env:REQUIRE_DB=1; pytest tests/test_research_schema.py
    bash:         REQUIRE_DB=1 pytest tests/test_research_schema.py

`$env:` persists for the rest of that PowerShell window. On the
workstation that is what you want. Clear it with
`Remove-Item Env:REQUIRE_DB` before working offline, or these turn from
skips into failures.
"""
from __future__ import annotations

import os

import pytest

from src.env import bootstrap

bootstrap()

REQUIRE_DB = bool(os.environ.get("REQUIRE_DB"))

TABLES = ("research_organizations", "research_projects",
          "research_project_taxonomy")


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        msg = ("DATABASE_URL is not set in the environment and was not "
               "found in .env, so the live schema cannot be checked")
        if REQUIRE_DB:
            pytest.fail(msg + " — REQUIRE_DB is set.")
        pytest.skip(msg)
    from src.db.writer import get_engine
    return get_engine()


@pytest.fixture(scope="module")
def conn():
    from sqlalchemy import text
    engine = _connect()
    try:
        c = engine.connect()
    except Exception as exc:                                 # noqa: BLE001
        msg = f"database unreachable: {type(exc).__name__}"
        if REQUIRE_DB:
            pytest.fail(msg + " — REQUIRE_DB is set.")
        pytest.skip(msg)
    present = c.execute(text(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name = ANY(:t)"
    ), {"t": list(TABLES)}).scalar()
    if present != len(TABLES):
        c.close()
        msg = (f"015_research_activity.sql is not applied "
               f"({present}/{len(TABLES)} tables present)")
        if REQUIRE_DB:
            pytest.fail(msg + " — REQUIRE_DB is set.")
        pytest.skip(msg)
    yield c
    c.close()


#: A project row these tests can hang a taxonomy row off. Negative ids so
#: nothing here can collide with a real TechPort project.
_PARENT_ID = -424242


def _refuses(conn, sql: str, *, with_parent: bool = False) -> bool:
    """
    True if the statement is rejected. Always rolls back.

    `with_parent` creates the referenced project first, and it is not a
    convenience - it is the difference between this file testing what it
    says it tests and testing nothing.

    THE BUG THIS ARGUMENT FIXES, FOUND 2026-09-15
    =============================================
    The tx_code assertions originally inserted against a techport_id with
    no row in `research_projects`. Every one of them passed - and kept
    passing after `research_tx_code_shape` was DROPPED, because the
    FOREIGN KEY refused the row before the CHECK was ever evaluated.
    Five green assertions, none of them touching the control they named.

    Found by deleting the constraint and expecting the suite to go red.
    It did not. **A control that has never been seen to fail is not
    evidence** - the same finding this project recorded in September
    about a health check that watched three pipelines out of five.
    """
    from sqlalchemy import text
    tx = conn.begin_nested()
    try:
        if with_parent:
            conn.execute(text(
                "INSERT INTO research_projects (techport_id, title) "
                "VALUES (:i, 'parent for a constraint test') "
                "ON CONFLICT (techport_id) DO NOTHING"
            ), {"i": _PARENT_ID})
        conn.execute(text(sql))
    except Exception:                                        # noqa: BLE001
        tx.rollback()
        return True
    tx.rollback()
    return False


# ── AD-086: the taxonomy code is a code, not a title ─────────────────

@pytest.mark.parametrize("bad,why", [
    ("Sensors and Instruments", "a node TITLE in the code column"),
    ("tx08.1", "lowercase"),
    ("11.6.4", "the number without the TX prefix"),
    ("TX8", "one digit where the taxonomy uses two"),
    ("", "empty"),
])
def test_tx_code_shape_is_enforced(conn, bad: str, why: str) -> None:
    sql = ("INSERT INTO research_project_taxonomy "
           "(techport_id, tx_code, tx_title) VALUES "
           f"({_PARENT_ID}, '{bad}', 'x')")
    assert _refuses(conn, sql, with_parent=True), (
        f"the database accepted {bad!r} as a tx_code ({why}). "
        f"A taxonomy column mixing codes and titles groups silently and "
        f"wrongly — see AD-086."
    )


def test_a_valid_tx_code_is_accepted(conn) -> None:
    """
    The positive half, and it is what makes the five negative assertions
    above mean something. Without it they would still all pass if the
    table refused every insert for an unrelated reason - which is exactly
    the bug `with_parent` was added to fix.
    """
    from sqlalchemy import text
    tx = conn.begin_nested()
    conn.execute(text(
        "INSERT INTO research_projects (techport_id, title) "
        "VALUES (:i, 'parent') ON CONFLICT (techport_id) DO NOTHING"
    ), {"i": _PARENT_ID})
    conn.execute(text(
        "INSERT INTO research_project_taxonomy "
        "(techport_id, tx_code, tx_title) VALUES (:i, 'TX11.6.4', 'Quantum Computer')"
    ), {"i": _PARENT_ID})
    top = conn.execute(text(
        "SELECT tx_top FROM research_project_taxonomy WHERE techport_id = :i"
    ), {"i": _PARENT_ID}).scalar()
    tx.rollback()
    assert top == "TX11", f"tx_top derived as {top!r}, expected 'TX11'"


def test_tx_top_is_generated_not_supplied(conn) -> None:
    from sqlalchemy import text
    col = conn.execute(text(
        "SELECT is_generated FROM information_schema.columns "
        "WHERE table_name='research_project_taxonomy' AND column_name='tx_top'"
    )).scalar()
    assert col == "ALWAYS", (
        "tx_top must be GENERATED. A column an importer fills is a column "
        "that can disagree with the code it was derived from."
    )


# ── AD-085: the link is optional, and its provenance is not ──────────

def test_link_without_a_method_is_refused(conn) -> None:
    assert _refuses(conn, (
        "INSERT INTO research_organizations (name, name_norm, "
        "organization_code) VALUES ('t','t-no-method','GSFC')"
    )), ("an organization_code with no match_method makes a loosened "
         "match indistinguishable from a strict one")


def test_method_without_a_link_is_refused(conn) -> None:
    assert _refuses(conn, (
        "INSERT INTO research_organizations (name, name_norm, "
        "match_method) VALUES ('t','t-no-code','strict')"
    ))


def test_a_performer_with_no_link_is_accepted(conn) -> None:
    """
    The normal case, and the whole point of AD-085: ~97% of TechPort
    performers do not appear in the operator catalogue. If this ever
    fails, the attribute has become the spine.
    """
    from sqlalchemy import text
    tx = conn.begin_nested()
    conn.execute(text(
        "INSERT INTO research_organizations (name, name_norm) "
        "VALUES ('Thinkorbital Inc.','thinkorbital')"
    ))
    tx.rollback()


# ── AD-061: a TRL is a project's, and it may be absent ───────────────

@pytest.mark.parametrize("trl", [0, 10, -1])
def test_trl_outside_one_to_nine_is_refused(conn, trl: int) -> None:
    assert _refuses(conn, (
        "INSERT INTO research_projects (techport_id, title, trl_current) "
        f"VALUES (-1, 'x', {trl})"
    ))


def test_a_project_with_no_trl_is_accepted(conn) -> None:
    """24% of TechPort projects state none, and they still belong here."""
    from sqlalchemy import text
    tx = conn.begin_nested()
    conn.execute(text(
        "INSERT INTO research_projects (techport_id, title) "
        "VALUES (-99, 'No TRL stated')"
    ))
    tx.rollback()


# ── The read surface is public, and only readable ────────────────────

@pytest.mark.parametrize("rel", TABLES + (
    "research_activity_by_area", "research_organization_summary"))
def test_anon_has_select_and_nothing_else(conn, rel: str) -> None:
    from sqlalchemy import text
    rows = conn.execute(text(
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee='anon' AND table_name=:r"
    ), {"r": rel}).scalars().all()
    extra = sorted(set(rows) - {"SELECT"})
    assert not extra, f"anon holds {extra} on {rel}"


@pytest.mark.parametrize("view", ("research_activity_by_area",
                                  "research_organization_summary"))
def test_views_run_as_the_invoker(conn, view: str) -> None:
    """
    The defect 010 found: a view without `security_invoker` runs with its
    OWNER's privileges, and matches `CREATE TABLE` in every audit that
    filters on relkind='r' — so it is invisible exactly where it matters.
    """
    from sqlalchemy import text
    opts = conn.execute(text(
        "SELECT reloptions FROM pg_class WHERE relname = :v"
    ), {"v": view}).scalar()
    assert opts and any("security_invoker=true" in o for o in opts), (
        f"{view} does not set security_invoker"
    )
