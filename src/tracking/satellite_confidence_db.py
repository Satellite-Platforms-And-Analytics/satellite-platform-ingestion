"""
satellite_confidence_db.py — Permanent cross-run satellite confidence store.

WHY THIS EXISTS
================
The historical_accuracy.py scoring pipeline produces per-run Excel reports
with timestamped filenames (e.g. historical_accuracy_report_2026-08-05_
1100-2300Z.xlsx). When historical_accuracy.py runs the next time with a
different date or time window, it can't find the previous report under the
new filename, so it re-evaluates every satellite from scratch even though
nothing about the satellite's orbital history has changed.

This module provides a permanent SQLite database that stores scoring results
for every satellite ever evaluated. Each run reads from it first (pulling
carried-forward results for all previously-scored satellites) and writes to
it last (saving new results for newly-scored satellites). The per-run Excel
reports still generate as before, but they are now populated from this
permanent store rather than just from the current run.

A satellite's result is re-evaluated (not carried forward) only when:
  1. It has never been scored before
  2. Its result was "Query failed" (transient network failure)
  3. Its result was "Insufficient historical data" (new data may now exist)
  4. The catalog-change detector flags it as having a significant orbital
     maneuver since the last evaluation (this is checked by main.py, not here)

STORAGE
========
Stored in TLE_DATA_DIR alongside the TLE history cache, so all persistent
satellite data is in one place for backup and portability.

The database is append-friendly: new satellites are inserted, existing ones
are only updated when the confidence category changes, so it is safe to run
concurrent processes (though the tool doesn't do this by design).
"""

import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS satellite_confidence (
    norad_id            INTEGER PRIMARY KEY,
    target_name         TEXT,
    target_orbit        TEXT,
    confidence_score    REAL,
    confidence_category TEXT,
    orbit_behavior      TEXT,
    historical_points   INTEGER,
    lookback_used_years REAL,
    launch_date         TEXT,
    country             TEXT,
    object_type         TEXT,
    launch_site         TEXT,
    intl_designator     TEXT,
    last_evaluated_at   TEXT NOT NULL,
    evaluation_tag      TEXT,
    data_json           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sc_category
    ON satellite_confidence (confidence_category);

CREATE INDEX IF NOT EXISTS idx_sc_evaluated
    ON satellite_confidence (last_evaluated_at);
"""

# Categories that should NOT be carried forward -- satellite should be
# re-evaluated on the next run since the "result" is really a failure mode.
_SKIP_CATEGORIES = {"Query failed"}

# Categories that CAN be carried forward but will be re-evaluated if new
# cache data is available (i.e. the delta sync may have filled in data).
_RECHECK_IF_DATA_AVAILABLE = {"Insufficient historical data"}


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@contextmanager
def _connect(db_path):
    _dir = os.path.dirname(db_path)
    if _dir:
        os.makedirs(_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def load_all_results(db_path):
    """
    Load all previously-scored satellite results from the permanent DB.

    Returns a dict: {norad_id (int): row_dict} for every satellite in the
    database whose confidence category is not in _SKIP_CATEGORIES.

    This is called at the start of historical_accuracy.py's main() before
    any Space-Track queries are made, so previously-scored satellites can
    be carried forward without re-evaluation.
    """
    if not os.path.exists(db_path):
        return {}

    results = {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM satellite_confidence"
        ).fetchall()
        for row in rows:
            d = dict(row)
            if d.get("confidence_category") in _SKIP_CATEGORIES:
                continue
            norad = d["norad_id"]
            # Reconstruct the column names that historical_accuracy.py uses
            results[norad] = _db_row_to_report_row(d)
    return results


def save_results(db_path, results, evaluation_tag=""):
    """
    Save or update scored satellite results in the permanent DB.

    results: list of row dicts from historical_accuracy.py's scoring
    pipeline (the same dicts that go into the Excel report).
    evaluation_tag: the run tag (e.g. "2026-08-05_1100-2300Z") to
    record which run produced each result.

    Existing rows are replaced only if the new result is "better"
    (i.e. has a real confidence score vs. was previously insufficient).
    Query-failed results are never persisted to the DB -- they stay
    transient and the satellite will be re-tried on the next run.
    """
    if not results:
        return

    now = _utc_now()
    rows_to_upsert = []

    for row in results:
        category = str(row.get("Confidence Category", ""))
        if category in _SKIP_CATEGORIES:
            continue  # never persist failures

        norad_id = int(row.get("Target NORAD", 0))
        if not norad_id:
            continue

        rows_to_upsert.append((
            norad_id,
            str(row.get("Target Name", "")),
            str(row.get("Target Orbit", "")),
            _safe_float(row.get("Confidence Score (0-100)")),
            category,
            str(row.get("Orbit Behavior", "")),
            _safe_int(row.get("Historical Data Points")),
            _safe_float(row.get("Lookback Used (years)")),
            str(row.get("Launch Date", "")),
            str(row.get("Country", "")),
            str(row.get("Object Type", "")),
            str(row.get("Launch Site", "")),
            str(row.get("International Designator", "")),
            now,
            evaluation_tag,
        ))

    if not rows_to_upsert:
        return

    with _connect(db_path) as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO satellite_confidence (
                norad_id, target_name, target_orbit,
                confidence_score, confidence_category,
                orbit_behavior, historical_points, lookback_used_years,
                launch_date, country, object_type, launch_site,
                intl_designator, last_evaluated_at, evaluation_tag
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows_to_upsert)


def needs_recheck(db_path, norad_id, tle_cache_db=None):
    """
    Return True if a satellite should be re-evaluated despite having
    an existing result in the permanent DB.

    A satellite is re-checked if:
      - Its category is "Insufficient historical data" AND the TLE
        history cache now has data for it (the delta sync may have
        filled it in since the last run).
      - Its result is from > 90 days ago (monthly refresh for active sats).

    This is called per-satellite during the skip/evaluate decision in
    historical_accuracy.py to ensure the permanent DB doesn't freeze
    results forever for satellites that genuinely have new information.
    """
    if not os.path.exists(db_path):
        return False

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT confidence_category, last_evaluated_at "
            "FROM satellite_confidence WHERE norad_id = ?",
            (norad_id,)
        ).fetchone()

    if not row:
        return True  # not in DB at all

    category = row["confidence_category"]

    if category in _SKIP_CATEGORIES:
        return True  # always retry failures

    if category in _RECHECK_IF_DATA_AVAILABLE and tle_cache_db:
        # Check if the TLE cache now has data for this satellite
        import tle_history_cache
        cached = tle_history_cache.load_cached_elements(tle_cache_db, [norad_id])
        if cached.get(norad_id):
            return True  # now has data, should re-score

    return False


def db_stats(db_path):
    """Return a human-readable summary of the permanent confidence DB."""
    if not os.path.exists(db_path):
        return "Satellite confidence DB: not yet created."

    with _connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM satellite_confidence"
        ).fetchone()[0]
        by_cat = conn.execute(
            "SELECT confidence_category, COUNT(*) as n "
            "FROM satellite_confidence GROUP BY confidence_category "
            "ORDER BY n DESC"
        ).fetchall()
        newest = conn.execute(
            "SELECT MAX(last_evaluated_at) FROM satellite_confidence"
        ).fetchone()[0]

    lines = [f"Satellite confidence DB: {total:,} satellites scored"]
    for row in by_cat:
        lines.append(f"  {row['confidence_category']}: {row['n']:,}")
    lines.append(f"  Last updated: {newest[:10] if newest else 'never'}")
    return "\n".join(lines)


# ── Internal helpers ──────────────────────────────────────────────────

def _safe_float(v):
    try:
        return float(v) if v is not None and str(v) not in ("nan", "", "None") else None
    except (ValueError, TypeError):
        return None


def _safe_int(v):
    try:
        return int(v) if v is not None and str(v) not in ("nan", "", "None") else None
    except (ValueError, TypeError):
        return None


def _db_row_to_report_row(d):
    """Convert a DB row dict back to the column names used in the Excel report."""
    return {
        "Target NORAD":              d["norad_id"],
        "Target Name":               d.get("target_name", ""),
        "Target Orbit":              d.get("target_orbit", ""),
        "Confidence Score (0-100)":  d.get("confidence_score"),
        "Confidence Category":       d.get("confidence_category", ""),
        "Orbit Behavior":            d.get("orbit_behavior", ""),
        "Historical Data Points":    d.get("historical_points"),
        "Lookback Used (years)":     d.get("lookback_used_years"),
        "Launch Date":               d.get("launch_date", ""),
        "Country":                   d.get("country", ""),
        "Object Type":               d.get("object_type", ""),
        "Launch Site":               d.get("launch_site", ""),
        "International Designator":  d.get("intl_designator", ""),
        "_last_evaluated_at":        d.get("last_evaluated_at", ""),
        "_evaluation_tag":           d.get("evaluation_tag", ""),
    }
