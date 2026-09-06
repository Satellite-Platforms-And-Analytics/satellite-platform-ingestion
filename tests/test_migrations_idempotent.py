"""
Every migration must be safe to run twice.

WHY
===
The schema directory is the only description of how to rebuild this
database. That claim is only true if the files can actually be applied to
a database that has already seen them — on a fresh project, a staging
copy, or a restore.

On 2026-09-05, while evaluating Supabase's GitHub integration (which
auto-applies migrations on push), a scan found `001_core_schema.sql`
could not be re-run: six `CREATE POLICY` statements with no preceding
`DROP POLICY IF EXISTS`. `CREATE POLICY` has no `IF NOT EXISTS` form, so
a second application fails partway — after some statements have already
taken effect. 006 was written with the drops; 001 predated the habit.

The integration was not adopted, but the fragility was real either way,
and it was invisible because nothing ever re-ran a migration.

This test reuses `split_statements` from the infrastructure repo's
`apply_migration.py` rather than reimplementing it, so the two cannot
disagree about where a statement ends — which they would, since naive
splitting on `;` shreds dollar-quoted function bodies.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re

import pytest

SCHEMA_DIR = pathlib.Path(
    os.environ.get("SCHEMA_SQL_PATH")
    or pathlib.Path(__file__).resolve().parents[2]
    / "satellite-platform-infrastructure" / "schema" / "001_core_schema.sql"
).parent

RUNNER = SCHEMA_DIR.parent / "apply_migration.py"
REQUIRE_SCHEMA = bool(os.environ.get("REQUIRE_SCHEMA"))

#: Forms that are safe to apply to a database that already has them.
IDEMPOTENT = re.compile(
    r"^\s*(CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX|EXTENSION|SCHEMA)"
    r"\s+IF\s+NOT\s+EXISTS"
    r"|CREATE\s+OR\s+REPLACE\s+(FUNCTION|VIEW|PROCEDURE|TRIGGER)"
    r"|ALTER\s+TABLE\s+\w+\s+(ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS"
    r"|ENABLE\s+ROW|DISABLE\s+ROW)"
    r"|ALTER\s+DEFAULT\s+PRIVILEGES"
    r"|DROP\s+\w+(\s+\w+)?\s+IF\s+EXISTS"
    r"|COMMENT\s+ON|GRANT|REVOKE)", re.I | re.S)

_DROP_POLICY = re.compile(r'DROP POLICY IF EXISTS "([^"]+)" ON (\w+)', re.I)
_CREATE_POLICY = re.compile(r'CREATE POLICY "([^"]+)" ON (\w+)', re.I)


def _split():
    if not RUNNER.exists():
        msg = f"apply_migration.py not found at {RUNNER}"
        if REQUIRE_SCHEMA:
            pytest.fail(msg + " — REQUIRE_SCHEMA is set.")
        pytest.skip(msg)
    spec = importlib.util.spec_from_file_location("_apply_migration", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.split_statements


def _migrations():
    if not SCHEMA_DIR.exists():
        msg = f"schema directory not found at {SCHEMA_DIR}"
        if REQUIRE_SCHEMA:
            pytest.fail(msg + " — REQUIRE_SCHEMA is set.")
        pytest.skip(msg)
    files = sorted(SCHEMA_DIR.glob("*.sql"))
    assert files, f"no migrations in {SCHEMA_DIR}"
    return files


def _offenders(path, split) -> list:
    """Statements in one file that a second application would break on."""
    dropped, bad = set(), []
    for st in split(path.read_text(encoding="utf-8")):
        one = " ".join(st.split())

        m = _DROP_POLICY.match(one)
        if m:
            dropped.add((m.group(1), m.group(2)))
            continue

        m = _CREATE_POLICY.match(one)
        if m:
            # CREATE POLICY has no IF NOT EXISTS. The only way to make it
            # repeatable is to drop it first, in the same file.
            if (m.group(1), m.group(2)) not in dropped:
                bad.append(one[:120])
            continue

        if IDEMPOTENT.match(st):
            continue
        if "ON CONFLICT" in one.upper() or "IF NOT EXISTS" in one.upper():
            continue
        bad.append(one[:120])
    return bad


def test_there_are_migrations_to_check():
    _migrations()


def test_every_migration_can_be_applied_twice():
    split = _split()
    failures = {}
    for path in _migrations():
        bad = _offenders(path, split)
        if bad:
            failures[path.name] = bad
    assert not failures, (
        "these statements would fail on a second application, so the "
        "schema directory cannot rebuild the database it describes:\n"
        + "\n".join(f"  {f}: {s}"
                    for f, ss in failures.items() for s in ss))


def test_every_create_policy_is_preceded_by_its_drop():
    # Called out separately because it is the specific defect found in
    # 001 and the one most likely to recur — a new table's policy is
    # easy to write without the drop.
    split = _split()
    for path in _migrations():
        text = path.read_text(encoding="utf-8")
        dropped = set(_DROP_POLICY.findall(" ".join(text.split())))
        for st in split(text):
            m = _CREATE_POLICY.match(" ".join(st.split()))
            if m:
                assert (m.group(1), m.group(2)) in dropped, (
                    f'{path.name}: CREATE POLICY "{m.group(1)}" on '
                    f'{m.group(2)} has no DROP POLICY IF EXISTS before it. '
                    f'CREATE POLICY has no IF NOT EXISTS form.')
