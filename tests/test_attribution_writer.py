"""
The enrichment writer: it must never create a satellite, and never write
a descriptive value without provenance.

Both rules exist because of what SATCAT is. It holds 70,580 records
against a catalogue of ~18,000, and 35,542 of those are objects that
re-entered years ago. An enrichment pass that inserted would quadruple
the table on a 500 MB tier as a side effect - a scope decision disguised
as a data-quality one.

These tests read the generated SQL rather than touching a database, so
they run in CI without credentials.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from src.db import writer


def valid_row(**over):
    row = {
        "norad_id": 25544, "object_type": "PAYLOAD", "status": "ACTIVE",
        "owner_code": "US", "country_code": "USA", "rcs_size": "LARGE",
        "launch_date": "1998-11-20", "launch_site": "TTMTR",
        "period_min": 92.8, "inclination_deg": 51.6,
        "apogee_km": 421.0, "perigee_km": 412.0,
        "data_source": "celestrak_satcat", "match_method": "norad_id",
        "source_confidence": 1.0,
        "matched_at": datetime.now(timezone.utc),
    }
    row.update(over)
    return row


# ── It must not create satellites ─────────────────────────────────────

def test_the_statement_is_an_update_not_an_insert():
    sql = writer._UPDATE_ATTRIBUTION_SQL.upper()
    assert sql.strip().startswith("UPDATE")
    assert "INSERT" not in sql, (
        "enrichment must never create a satellite - SATCAT holds 70,580 "
        "records against a catalogue of ~18,000, most of them decayed")


def test_it_joins_on_norad_id_so_unmatched_records_do_nothing():
    assert re.search(r"WHERE\s+s\.norad_id\s*=\s*v\.norad_id",
                     writer._UPDATE_ATTRIBUTION_SQL)


# ── Provenance is mandatory ───────────────────────────────────────────

def test_a_row_without_data_source_is_rejected():
    with pytest.raises(ValueError, match="no data_source"):
        writer.upsert_satellite_attribution([valid_row(data_source=None)])


def test_a_row_without_norad_id_is_rejected():
    with pytest.raises(ValueError, match="missing norad_id"):
        writer.upsert_satellite_attribution([valid_row(norad_id=None)])


def test_provenance_is_assigned_not_coalesced():
    # Provenance describes THIS pass. Preserving an older value would
    # claim the row's attribution came from a source that did not write it.
    for col in writer._ATTRIBUTION_PROVENANCE:
        assert re.search(rf"\b{col} = v\.{col}\b",
                         writer._UPDATE_ATTRIBUTION_SQL), col
        assert f"{col} = COALESCE" not in writer._UPDATE_ATTRIBUTION_SQL


def test_descriptive_columns_are_coalesced_not_overwritten():
    # A source that omits a field must not blank one another source filled.
    for col in writer._ATTRIBUTION_DESCRIPTIVE:
        assert f"{col} = COALESCE(v.{col}, s.{col})" in \
            writer._UPDATE_ATTRIBUTION_SQL, col


def test_the_two_column_groups_do_not_overlap():
    assert not (set(writer._ATTRIBUTION_DESCRIPTIVE)
                & set(writer._ATTRIBUTION_PROVENANCE))


# ── Casts ─────────────────────────────────────────────────────────────

def test_every_value_carries_an_explicit_cast():
    # execute_values sends VALUES tuples untyped, and Postgres will not
    # coerce text into DATE/REAL/TIMESTAMPTZ inside UPDATE ... FROM.
    n = len(writer._ATTRIBUTION_COLUMNS)
    assert writer._ATTRIBUTION_VALUES_TEMPLATE.count("%s::") == n


def test_the_typed_columns_are_typed_correctly():
    types = dict(writer._ATTRIBUTION_COLUMNS)
    assert types["norad_id"] == "int"
    assert types["launch_date"] == "date"
    assert types["matched_at"] == "timestamptz"
    for numeric in ("period_min", "inclination_deg", "apogee_km",
                    "perigee_km", "source_confidence"):
        assert types[numeric] == "real", numeric


def test_template_arity_matches_the_column_list():
    assert (writer._ATTRIBUTION_VALUES_TEMPLATE.count("%s")
            == len(writer._ATTRIBUTION_COLUMNS))


# ── It stays out of the fetcher's way ─────────────────────────────────

def test_enrichment_does_not_touch_the_orbital_columns():
    # The 2-hourly CelesTrak fetch owns these. Two writers on one table
    # is only safe while their column sets are disjoint.
    fetcher_owned = {"tle_line1", "tle_line2", "tle_epoch", "mean_motion",
                     "eccentricity", "orbit_regime", "name",
                     "intl_designator"}
    enriched = {c for c, _ in writer._ATTRIBUTION_COLUMNS}
    assert not (fetcher_owned & enriched)


def test_empty_input_writes_nothing():
    assert writer.upsert_satellite_attribution([]) == 0


# ── The count must be rows AFFECTED, not rows submitted ───────────────
#
# _bulk_upsert returns len(rows) by default. For this writer that would
# report "enriched 70,580" against a catalogue of ~18,000 and compute a
# coverage gap of zero.

import inspect


def test_attribution_asks_for_the_affected_count():
    src = inspect.getsource(writer.upsert_satellite_attribution)
    assert "count_affected=True" in src, (
        "without this the writer reports rows submitted, so every "
        "unmatched SATCAT record would be counted as an enriched satellite")


def test_bulk_upsert_supports_counting_affected_rows():
    sig = inspect.signature(writer._bulk_upsert)
    assert "count_affected" in sig.parameters
    assert sig.parameters["count_affected"].default is False, (
        "the default must stay False so existing callers are unchanged")


def test_the_affected_path_pages_and_sums():
    # execute_values pages internally and leaves cur.rowcount reflecting
    # only the last page, so a true total needs paging here.
    src = inspect.getsource(writer._bulk_upsert)
    assert "affected += max(cur.rowcount, 0)" in src
    assert "for i in range(0, len(rows), page_size)" in src
