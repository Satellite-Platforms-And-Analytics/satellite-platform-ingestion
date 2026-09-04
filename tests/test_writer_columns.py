"""
Guards the batched writers against schema drift.

`execute_values` positions values by tuple order. If a writer's column list
stops matching the real table, values land in the wrong columns - and with
ON CONFLICT DO UPDATE that corrupts existing rows silently: no error, no
constraint violation, just wrong data.

NOTE ON WHAT IS WORTH TESTING. The first version of this file compared each
writer's column list against its own INSERT statement. That is tautological:
the SQL is an f-string built FROM the column list, so they cannot disagree.
Swapping two columns still passed. The meaningful invariant is the writer
against 001_core_schema.sql - the one thing that can actually drift, because
the schema lives in a different repository.

No database, no network - the schema is parsed from the .sql file.
"""
import os
import pathlib
import re

import pytest

pytest.importorskip("sqlalchemy", reason="writer.py requires sqlalchemy")

from src.db import writer  # noqa: E402

#: 001_core_schema.sql lives in the sibling infrastructure repo. CI checks
#: that repo out somewhere else, so allow an explicit override.
SCHEMA = pathlib.Path(
    os.environ.get("SCHEMA_SQL_PATH")
    or (pathlib.Path(__file__).resolve().parents[2]
        / "satellite-platform-infrastructure" / "schema" / "001_core_schema.sql")
)

#: Set in CI. Turns "schema not available, skip" into a hard failure.
#:
#: These three tests are the only guard against writer.py and the schema
#: drifting apart, and the schema lives in a different repository - so the
#: default is to skip when it is absent, which is right on a machine that
#: only has one repo checked out. In CI that same skip would mean the
#: guard silently never runs while the job reports green, which is the
#: failure mode this project keeps hitting. Make it loud there.
REQUIRE_SCHEMA = bool(os.environ.get("REQUIRE_SCHEMA"))


def _require_schema() -> None:
    if SCHEMA.exists():
        return
    message = (f"001_core_schema.sql not found at {SCHEMA}. Check out "
               f"satellite-platform-infrastructure beside this repo, or set "
               f"SCHEMA_SQL_PATH.")
    if REQUIRE_SCHEMA:
        pytest.fail(message + " REQUIRE_SCHEMA is set, so this guard must run.")
    pytest.skip(message)


def _table_columns(table: str) -> list:
    """Column names for one CREATE TABLE, in declaration order."""
    _require_schema()
    sql = SCHEMA.read_text(encoding="utf-8")
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);",
                  sql, re.S | re.I)
    assert m, f"{table} not found in {SCHEMA.name}"

    cols = []
    for line in m.group(1).splitlines():
        line = line.split("--")[0].strip()
        if not line or line.upper().startswith(("UNIQUE", "PRIMARY KEY",
                                                "FOREIGN KEY", "CONSTRAINT",
                                                "CHECK")):
            continue
        name = line.split()[0].strip(",")
        if name:
            cols.append(name)
    return cols


@pytest.mark.parametrize("writer_cols,table,generated", [
    (writer._SATELLITE_COLUMNS, "satellites", {"id", "last_updated", "created_at"}),
    (writer._VISIBILITY_COLUMNS, "visibility_windows", {"id", "created_at"}),
    (writer._TLE_HISTORY_COLUMNS, "tle_history", {"id", "fetched_at"}),
], ids=["satellites", "visibility_windows", "tle_history"])
def test_writer_columns_match_schema(writer_cols, table, generated):
    """
    Every column the writer names must exist in the table, and every column
    the table requires must be written or generated. `generated` lists the
    ones the database fills itself.
    """
    schema_cols = _table_columns(table)
    unknown = [c for c in writer_cols if c not in schema_cols]
    assert not unknown, f"{table}: writer names columns not in the schema: {unknown}"

    unwritten = [c for c in schema_cols
                 if c not in writer_cols and c not in generated]
    assert not unwritten, (
        f"{table}: schema has columns the writer never sets: {unwritten}. "
        "Add them to the writer's column list, or to `generated` if the "
        "database fills them."
    )


def test_visibility_payload_keys_match_column_list():
    """
    insert_visibility_windows builds dicts, then converts them to tuples with
    `tuple(row[c] for c in _VISIBILITY_COLUMNS)`. A key present in one and not
    the other raises KeyError at runtime, inside a scheduled job. Catch it here.
    """
    src = pathlib.Path(writer.__file__).read_text(encoding="utf-8")
    body = src[src.index("def insert_visibility_windows"):
               src.index("def upsert_imagery_scene")]
    payload_block = body[body.index("payload.append({"):body.index("})", body.index("payload.append({"))]
    keys = set(re.findall(r'"(\w+)":', payload_block))
    assert keys == set(writer._VISIBILITY_COLUMNS), (
        f"payload keys and _VISIBILITY_COLUMNS disagree.\n"
        f"  only in payload: {keys - set(writer._VISIBILITY_COLUMNS)}\n"
        f"  only in columns: {set(writer._VISIBILITY_COLUMNS) - keys}"
    )


def test_satellite_template_arity_matches_columns():
    placeholders = writer._SATELLITE_VALUES_TEMPLATE.count("%s")
    assert placeholders == len(writer._SATELLITE_COLUMNS)
    assert "now()" in writer._SATELLITE_VALUES_TEMPLATE


@pytest.mark.parametrize("sql,name", [
    (writer._UPSERT_SATELLITE_SQL, "satellites"),
    (writer._UPSERT_VISIBILITY_SQL, "visibility_windows"),
    (writer._INSERT_TLE_HISTORY_SQL, "tle_history"),
])
def test_batched_sql_uses_values_placeholder(sql, name):
    """
    execute_values needs a single `VALUES %s`. A statement reverted to named
    parameters would silently fall back to the per-row path that cost a week
    of TLE writes in August 2026.
    """
    assert "VALUES %s" in sql, f"{name} is not in execute_values form"


def test_bulk_upsert_rejects_wrong_arity():
    with pytest.raises(ValueError, match="arity"):
        writer._bulk_upsert("INSERT INTO t (a, b) VALUES %s",
                            [(1, 2), (3,)], template="(%s, %s)")
