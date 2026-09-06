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


def _migration_files() -> list:
    """
    Every .sql in the schema directory, in filename order.

    The guard used to read 001_core_schema.sql alone. That was correct
    while it was the only file, and becomes wrong the moment a migration
    adds a column: writer.py would legitimately write `users`, the guard
    would look only at the original CREATE TABLE, and it would fail
    claiming the schema has no such column. A guard that fires on correct
    code is worse than no guard, because it gets disabled.
    """
    return sorted(SCHEMA.parent.glob("*.sql"))


_ADD_COLUMN = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)",
    re.I)


def _added_columns(table: str) -> list:
    """
    Columns introduced by ALTER TABLE ... ADD COLUMN in later migrations,
    in the order the migrations apply them.
    """
    found = []
    for path in _migration_files():
        sql = path.read_text(encoding="utf-8")
        # Strip comments first: 004 documents the columns it is *not*
        # adding, and those must not be mistaken for real ones.
        sql = "\n".join(l for l in sql.splitlines()
                         if not l.strip().startswith("--"))
        for stmt in re.split(r";", sql):
            m = re.search(rf"ALTER\s+TABLE\s+{table}\b", stmt, re.I)
            if not m:
                continue
            for col in _ADD_COLUMN.findall(stmt):
                if col not in found:
                    found.append(col)
    return found


def _table_columns(table: str) -> list:
    """
    Column names for one CREATE TABLE, in declaration order.

    Searches every migration, not just 001. The guard already knew that
    a later migration can ADD COLUMN to an existing table; it did not
    know a later migration can CREATE a table. 006_catalog_events.sql
    made that difference visible — the same blind spot one level up, and
    the same failure mode: a guard firing on correct code, which is worse
    than no guard because it gets disabled.
    """
    _require_schema()
    m = None
    for path in _migration_files():
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);",
                      path.read_text(encoding="utf-8"), re.S | re.I)
        if m:
            break
    assert m, (f"{table} is not created by any migration in "
               f"{SCHEMA.parent}")

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

    # Columns added by later migrations count too.
    cols.extend(c for c in _added_columns(table) if c not in cols)
    return cols


#: Columns owned by a writer other than the one under test.
#:
#: Until 2026-09-04 this test assumed one writer owned each table, which
#: was true. Migration 004 breaks that for `satellites`: the 2-hourly TLE
#: fetch owns the orbital columns, and a Phase 2 enrichment pass will own
#: attribution provenance. Listing them here is not a way to silence the
#: guard - the columns are still required to exist in the schema, and a
#: typo in this set fails the test. It records the ownership split so an
#: orphaned column cannot hide in it.
OWNED_BY_ENRICHMENT = {
    # 004_catalog_provenance.sql
    "users", "data_source", "match_method", "source_confidence",
    "matched_at",
    # 005_owner_code.sql - SATCAT's OWNER verbatim. Deliberately not in
    # _SATELLITE_COLUMNS: the 2-hourly CelesTrak fetch has no owner data
    # to write, and adding it there would mean passing NULL twelve times
    # a day into a column enrichment owns.
    "owner_code",
}


@pytest.mark.parametrize("writer_cols,table,generated,owned_elsewhere", [
    (writer._SATELLITE_COLUMNS, "satellites",
     {"id", "last_updated", "created_at"}, OWNED_BY_ENRICHMENT),
    (writer._VISIBILITY_COLUMNS, "visibility_windows",
     {"id", "created_at"}, set()),
    (writer._TLE_HISTORY_COLUMNS, "tle_history",
     {"id", "fetched_at"}, set()),
    # 006_catalog_events.sql. detected_at/updated_at are database
    # defaults; everything else this writer names.
    ([c for c, _ in writer._CATALOG_EVENT_COLUMNS], "catalog_events",
     {"id", "detected_at", "updated_at"}, set()),
], ids=["satellites", "visibility_windows", "tle_history",
        "catalog_events"])
def test_writer_columns_match_schema(writer_cols, table, generated,
                                     owned_elsewhere):
    """
    Every column the writer names must exist in the table, and every column
    in the table must be written by this writer, filled by the database, or
    explicitly owned by another writer.
    """
    schema_cols = _table_columns(table)
    unknown = [c for c in writer_cols if c not in schema_cols]
    assert not unknown, f"{table}: writer names columns not in the schema: {unknown}"

    # A column named as owned elsewhere must still exist - otherwise this
    # set becomes a place for typos and deleted columns to hide.
    missing = sorted(c for c in owned_elsewhere if c not in schema_cols)
    assert not missing, (
        f"{table}: OWNED_BY_ENRICHMENT names columns no schema file "
        f"declares: {missing}. This test reads schema/*.sql, so it means "
        f"the migration file is missing from the repo or the name is "
        f"misspelt - not that the migration has not been applied to the "
        f"database, which this test cannot see."
    )

    unwritten = [c for c in schema_cols
                 if c not in writer_cols
                 and c not in generated
                 and c not in owned_elsewhere]
    assert not unwritten, (
        f"{table}: schema has columns nothing writes: {unwritten}. Add them "
        "to the writer's column list, to `generated` if the database fills "
        "them, or to OWNED_BY_ENRICHMENT if another writer owns them."
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
