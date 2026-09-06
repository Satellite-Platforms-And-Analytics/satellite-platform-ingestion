"""
The catalogue events writer.

Two properties matter, and both exist because of how the monitor runs.

It sweeps a rolling 30-day window every day, so it re-detects the same
launches for weeks. If a re-run inserted instead of updating, one launch
would become thirty rows and every dashboard count built on this table
would be wrong — silently, in a direction that looks like activity.

And an ingestion change can cover 587 fragments. Writing a 587-element
array on each of thirty re-detections is how a small events table stops
being small, so the array is a sample and object_count is the number that
means something.
"""
from __future__ import annotations

import json
import re

import pytest

from src.db import writer


def event(**over):
    e = {
        "event_type": "fragmentation",
        "event_key": "1982-092:2026-09-01",
        "launch_key": "1982-092",
        "launch_date": "1982-09-16",
        "object_count": 3,
        "object_types": "3 DEBRIS",
        "norad_ids": [50032, 50058, 50621],
        "first_seen": "2026-09-01",
        "notable": True,
        "details": {"prior": 1},
    }
    e.update(over)
    return e


# ── Re-running must not duplicate ─────────────────────────────────────

def test_the_upsert_keys_on_type_and_natural_key():
    sql = writer._UPSERT_CATALOG_EVENT_SQL
    assert "ON CONFLICT (event_type, event_key) DO UPDATE" in sql, (
        "the monitor re-detects the same launch daily for a month; "
        "without this the table inflates and every count built on it lies")


def test_a_re_detection_refreshes_the_mutable_fields():
    sql = writer._UPSERT_CATALOG_EVENT_SQL
    for col in ("object_count", "object_types", "norad_ids", "notable",
                "details"):
        assert f"{col} = EXCLUDED.{col}" in sql, col
    assert "updated_at = now()" in sql


def test_the_key_columns_are_not_overwritten_by_the_update():
    sql = writer._UPSERT_CATALOG_EVENT_SQL
    assert "event_type = EXCLUDED.event_type" not in sql
    assert "event_key = EXCLUDED.event_key" not in sql


def test_an_event_without_a_key_is_rejected():
    with pytest.raises(ValueError, match="event_type and event_key"):
        writer.upsert_catalog_events([event(event_key=None)])
    with pytest.raises(ValueError, match="event_type and event_key"):
        writer.upsert_catalog_events([event(event_type="")])


# ── The array is a sample, the count is the truth ─────────────────────

def test_norad_ids_are_capped_but_the_count_is_not():
    captured = {}

    def fake(sql, rows, template=None, page_size=1000, count_affected=False):
        captured["rows"] = rows
        return len(rows)

    original = writer._bulk_upsert
    writer._bulk_upsert = fake
    try:
        writer.upsert_catalog_events([event(
            norad_ids=list(range(1000)), object_count=587)])
    finally:
        writer._bulk_upsert = original

    cols = [c for c, _ in writer._CATALOG_EVENT_COLUMNS]
    row = dict(zip(cols, captured["rows"][0]))
    assert len(row["norad_ids"]) == writer.MAX_NORAD_SAMPLE
    assert row["object_count"] == 587, (
        "object_count must survive the cap — it is the number the "
        "dashboard reports")


def test_details_are_serialised_for_jsonb():
    captured = {}

    def fake(sql, rows, template=None, page_size=1000, count_affected=False):
        captured["rows"] = rows
        return len(rows)

    original = writer._bulk_upsert
    writer._bulk_upsert = fake
    try:
        writer.upsert_catalog_events([event(details={"mean_lag_days": 6})])
    finally:
        writer._bulk_upsert = original

    cols = [c for c, _ in writer._CATALOG_EVENT_COLUMNS]
    row = dict(zip(cols, captured["rows"][0]))
    assert json.loads(row["details"]) == {"mean_lag_days": 6}


# ── Casts ─────────────────────────────────────────────────────────────

def test_every_value_carries_a_cast_and_the_arity_matches():
    n = len(writer._CATALOG_EVENT_COLUMNS)
    assert writer._CATALOG_EVENT_VALUES_TEMPLATE.count("%s::") == n
    assert writer._CATALOG_EVENT_VALUES_TEMPLATE.count("%s") == n


def test_the_awkward_types_are_cast_correctly():
    types = dict(writer._CATALOG_EVENT_COLUMNS)
    assert types["norad_ids"] == "integer[]"
    assert types["details"] == "jsonb"
    assert types["notable"] == "boolean"
    assert types["launch_date"] == "date"
    assert types["first_seen"] == "date"


def test_empty_input_writes_nothing():
    assert writer.upsert_catalog_events([]) == 0


# ── detected_at must never move ───────────────────────────────────────
#
# The monitor reports only events first seen in the last day, and it asks
# that question of detected_at. If a re-detection bumped detected_at, the
# same nineteen launches would look new every morning and the daily issue
# would repeat itself until nobody read it — the alert-fatigue version of
# a swallowed error.
#
# So this is load-bearing, not incidental: detected_at is set by the
# column default on INSERT and must be absent from the DO UPDATE list,
# while updated_at is refreshed in its place.

def test_detected_at_is_not_refreshed_by_a_re_detection():
    sql = writer._UPSERT_CATALOG_EVENT_SQL
    assert "detected_at = " not in sql, (
        "detected_at records when a finding was FIRST seen; the monitor's "
        "'what is new' question is asked of it, so a re-detection must "
        "leave it alone")
    assert "updated_at = now()" in sql, (
        "updated_at is what a re-detection should move instead")


def test_detected_at_is_not_a_column_this_writer_supplies():
    # It comes from the schema default. Passing it would let a caller
    # backdate a finding.
    assert "detected_at" not in [c for c, _ in writer._CATALOG_EVENT_COLUMNS]
    assert "updated_at" not in [c for c, _ in writer._CATALOG_EVENT_COLUMNS]


def test_the_reporter_asks_about_detected_at_not_updated_at():
    import inspect
    import report_events
    src = inspect.getsource(report_events.main)
    assert "detected_at >= now()" in src, (
        "querying updated_at would return every re-detected event every "
        "day, which is the thing this design exists to avoid")
    assert "updated_at >=" not in src
