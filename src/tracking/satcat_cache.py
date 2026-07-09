"""
Local 24-hour cache for Space-Track SATCAT (satellite catalog) data.

WHY THIS EXISTS
================
Space-Track's documented API usage policy
(https://www.space-track.org/documentation -> "API Use Guidelines" ->
"Retrieval Strategy" table) lists SATCAT as:

    SATCAT   1 / day   Once per day after 1700 (UTC) for SATCAT data.
    Follow best practices for downloading SATCAT daily.

Before this module existed, historical_accuracy.py queried SATCAT
fresh on EVERY run -- if the tool is run more than once a day (a
perfectly normal usage pattern: morning planning run, then an
updated run before an evening observation window), that's a direct
policy violation.

SATCAT data is fundamentally different from gp_history TLE data: it
describes mostly-static catalog metadata (launch date, country,
object type, launch site) that essentially never changes for an
already-catalogued object, with rare exceptions (a decay date getting
added once an object reenters). A 24-hour cache -- the same pattern
already used for GCAT in satellite_utils.py's fetch_gcat_catalog --
is the appropriate model here, not the permanent per-satellite cache
used for gp_history (see tle_history_cache.py for that one, and why
its requirements are different).

STORAGE FORMAT
===============
A single SQLite database, same rationale as tle_history_cache.py:
atomic writes, indexed lookups, one file instead of thousands of
small per-satellite files.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager


def _utc_now_naive():
    """
    UTC 'now' as a naive datetime -- see tle_history_cache.py's
    identical helper for the full explanation (matches the naive-
    datetime convention used throughout this codebase, while
    avoiding the deprecated datetime.utcnow() call).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS satcat_records (
    norad_id          INTEGER PRIMARY KEY,
    launch_date       TEXT,
    intl_designator   TEXT,
    object_type       TEXT,
    country           TEXT,
    launch_site       TEXT,
    size_class        TEXT,
    decay_date        TEXT,
    fetched_at        TEXT NOT NULL
);
"""


@contextmanager
def _connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def split_cached_vs_needed(db_path, norad_ids, max_age_hours=24):
    """
    Return (fresh_data, needs_fetch) where fresh_data is
    {norad_id: {...metadata...}} for every requested ID that has a
    cached SATCAT record fetched within the last max_age_hours, and
    needs_fetch is a list of NORAD IDs that are either uncached or
    whose cached record has gone stale.

    Unlike the TLE history cache (which only ever needs the
    INCREMENTAL slice since the last fetch), SATCAT records are
    either fresh enough to use as-is or need a full re-fetch -- there
    is no meaningful "partial" SATCAT record.
    """
    if not norad_ids:
        return {}, []

    cutoff = (_utc_now_naive() - timedelta(hours=max_age_hours)).isoformat()

    fresh_data = {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" * len(norad_ids))
        rows = conn.execute(
            f"SELECT norad_id, launch_date, intl_designator, object_type, "
            f"country, launch_site, size_class, decay_date, fetched_at "
            f"FROM satcat_records WHERE norad_id IN ({placeholders}) "
            f"AND fetched_at >= ?",
            list(norad_ids) + [cutoff],
        ).fetchall()
        for row in rows:
            norad_id = row[0]
            fresh_data[norad_id] = {
                "launch_date":     row[1] or "",
                "intl_designator": row[2] or "",
                "object_type":     row[3] or "",
                "country":         row[4] or "",
                "launch_site":     row[5] or "",
                "size_class":      row[6] or "",
                "decay_date":      row[7] or "",
            }

    needs_fetch = [nid for nid in norad_ids if nid not in fresh_data]
    return fresh_data, needs_fetch


def store_records(db_path, satcat_data):
    """
    Persist freshly-fetched SATCAT records to the local cache,
    stamped with the current time so split_cached_vs_needed can
    later tell whether they're still fresh enough to reuse.

    satcat_data: {norad_id: {"launch_date": ..., "intl_designator":
    ..., "object_type": ..., "country": ..., "launch_site": ...,
    "size_class": ..., "decay_date": ...}} -- same shape returned by
    satellite_utils.fetch_satcat_data.
    """
    if not satcat_data:
        return

    now_iso = _utc_now_naive().isoformat()
    with _connect(db_path) as conn:
        for norad_id, meta in satcat_data.items():
            conn.execute(
                "INSERT OR REPLACE INTO satcat_records "
                "(norad_id, launch_date, intl_designator, object_type, "
                " country, launch_site, size_class, decay_date, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    norad_id,
                    meta.get("launch_date", ""),
                    meta.get("intl_designator", ""),
                    meta.get("object_type", ""),
                    meta.get("country", ""),
                    meta.get("launch_site", ""),
                    meta.get("size_class", ""),
                    meta.get("decay_date", ""),
                    now_iso,
                ),
            )


def cache_stats(db_path, max_age_hours=24):
    """Short human-readable summary, for printing before SATCAT enrichment starts."""
    if not os.path.exists(db_path):
        return "Local SATCAT cache: not yet created (first run)."

    cutoff = (_utc_now_naive() - timedelta(hours=max_age_hours)).isoformat()
    with _connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM satcat_records").fetchone()[0]
        fresh = conn.execute(
            "SELECT COUNT(*) FROM satcat_records WHERE fetched_at >= ?",
            (cutoff,),
        ).fetchone()[0]

    return (
        f"Local SATCAT cache: {total:,} records total, {fresh:,} still "
        f"fresh (within {max_age_hours}h) -- those will NOT be re-queried."
    )
