"""
src/db/writer.py

Supabase (PostgreSQL) writer for the satellite-platform-ingestion pipeline.

This is the single place the pipeline talks to the database. Every other
module (src/tracking/, src/imagery/, src/db/) should call functions here
rather than opening its own connection, so connection handling and upsert
logic stay in one place.

Written directly against satellite-platform-infrastructure/schema/
001_core_schema.sql (Phase 1 core schema, 8 tables + 3 views). Column
names below match that file exactly as of 2026-07-09.

─────────────────────────────────────────────────────────────────────────
⚠️  KNOWN SCHEMA ISSUE — norad_id is still INTEGER, not BIGINT
─────────────────────────────────────────────────────────────────────────
ENB-003 and the 2026-07-10 daily note both flag this as critical: CelesTrak
exceeds 5-digit NORAD catalog numbers around July 12, 2026, and TLE format
can't represent 6-digit IDs at all (a separate problem — you'll need to
switch CelesTrak/Space-Track fetches to JSON format for that part). But
even once you're pulling 6-digit IDs from JSON, satellites.norad_id,
tle_history.norad_id, orbital_positions.norad_id, and
visibility_windows.norad_id are all still `INTEGER` (max ~2.1 billion, so
6-digit values *do* fit numerically — this isn't an overflow risk) — the
real risk is anything in this codebase or a future migration that assumes
5-digit/zero-padded norad_id formatting. This writer treats norad_id as a
plain Python int throughout, so it will handle 6-digit values correctly
as-is. No schema change is strictly required for INTEGER overflow, but if
you want the extra headroom, see the migration note at the bottom of this
file.

─────────────────────────────────────────────────────────────────────────
CALL ORDER MATTERS
─────────────────────────────────────────────────────────────────────────
tle_history, orbital_positions, and visibility_windows all have
`REFERENCES satellites(norad_id) ON DELETE CASCADE`. Call
upsert_satellites() for a given norad_id *before* writing TLE history,
positions, or visibility windows for it, or the insert will fail on the
foreign key constraint.

─────────────────────────────────────────────────────────────────────────
USAGE
─────────────────────────────────────────────────────────────────────────

    from src.db.writer import (
        upsert_satellites,
        insert_tle_history,
        upsert_orbital_positions,
        insert_visibility_windows,
        upsert_imagery_scene,
        new_run_id,
        log_step,
        ingestion_step,
        prune_old_positions,
    )

    run_id = new_run_id()
    with ingestion_step(run_id, pipeline="tle_fetch", step="validate"):
        upsert_satellites(satellite_rows)

Requires DATABASE_URL to be set (Supabase connection string, e.g.
postgresql://postgres:[password]@db.xxxx.supabase.co:5432/postgres —
use the *pooled* connection string, port 6543, for GitHub Actions).

Add to requirements.txt:  sqlalchemy  psycopg2-binary
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger("satellite_platform.db.writer")
if not logger.handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

_ENGINE: Optional[Engine] = None


# =====================================================
# ENGINE / CONNECTION
# =====================================================

def get_engine() -> Engine:
    """
    Lazily create (and cache) the SQLAlchemy engine.

    NullPool is deliberate: this module runs both in short-lived GitHub
    Actions jobs (one process, a handful of calls, then exit) and in
    longer local sessions. NullPool opens a fresh connection per checkout
    and closes it on release — simplest option, and avoids stale-connection
    errors after Supabase's free-tier compute pauses from inactivity.
    """
    global _ENGINE
    if _ENGINE is None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Set it to your Supabase Postgres "
                "connection string (Project Settings -> Database -> "
                "Connection string -> URI). Use the pooled/transaction "
                "connection string (port 6543) for GitHub Actions."
            )
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)

        _ENGINE = create_engine(database_url, poolclass=NullPool, future=True)
        logger.info("Database engine created (NullPool).")
    return _ENGINE


@contextmanager
def _tx():
    """Yield a connection inside a transaction; commits on success, rolls back on error."""
    engine = get_engine()
    with engine.connect() as conn:
        with conn.begin():
            yield conn


def check_connection() -> bool:
    """Quick connectivity check — returns True if SELECT 1 succeeds."""
    try:
        with _tx() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection OK.")
        return True
    except Exception as exc:
        logger.error("Database connection failed: %s", exc)
        return False


# =====================================================
# SATELLITES
# =====================================================

_SATELLITE_COLUMNS = [
    "norad_id", "name", "intl_designator", "country_code", "operator",
    "manufacturer", "purpose", "orbit_regime", "orbit_type", "launch_date",
    "launch_site", "launch_vehicle", "expected_lifetime_yr", "mass_kg",
    "perigee_km", "apogee_km", "inclination_deg", "period_min", "rcs_size",
    "status", "object_type", "tle_line1", "tle_line2", "tle_epoch",
    "mean_motion", "eccentricity", "source",
]

_UPSERT_SATELLITE_SQL = f"""
    INSERT INTO satellites (
        {", ".join(_SATELLITE_COLUMNS)}, last_updated
    ) VALUES %s
    ON CONFLICT (norad_id) DO UPDATE SET
        {", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, satellites.{c})"
            for c in _SATELLITE_COLUMNS if c != "norad_id"
        )},
        last_updated = now()
"""

#: execute_values substitutes this per row; now() is evaluated server-side.
_SATELLITE_VALUES_TEMPLATE = (
    "(" + ", ".join(["%s"] * len(_SATELLITE_COLUMNS)) + ", now())"
)


# ══════════════════════════════════════════════════════════════════════════════
#  Bulk write helper
# ══════════════════════════════════════════════════════════════════════════════
#
# SQLAlchemy cannot batch a textual (`text()`) statement. Passing a list of
# dicts to conn.execute(text(...), rows) falls through to plain
# cursor.executemany(), which is one network round-trip PER ROW. Against
# Supabase that measured ~14 rows/sec - 496 orbital positions took 35s, and
# a full 16,720-satellite pass would have blown the workflow timeout.
#
# psycopg2's execute_values collapses the whole batch into a single
# multi-row INSERT ... VALUES (...), (...), ... statement. Same SQL
# semantics, same ON CONFLICT behaviour, one round-trip per page.

def _bulk_upsert(sql_with_values: str,
                 rows: "list[tuple]",
                 template: "str | None" = None,
                 page_size: int = 1000,
                 count_affected: bool = False) -> int:
    """
    Execute a multi-row INSERT via psycopg2's execute_values.

    `sql_with_values` must contain a single `%s` placeholder where the
    VALUES tuples go, e.g.
        INSERT INTO t (a, b) VALUES %s ON CONFLICT (a) DO UPDATE SET ...

    `template` overrides the per-row tuple, for cases where a column is
    computed server-side (e.g. "(%s, %s, now())").

    `count_affected` changes what is RETURNED. By default this reports the
    number of rows submitted, which is fine for INSERT ... ON CONFLICT DO
    UPDATE, where every row either inserts or updates and the two numbers
    agree.

    They do NOT agree for a statement that can match nothing. The
    attribution writer sends 70,580 SATCAT records at a catalogue of
    ~18,000; rows for objects we do not track match no satellite and
    update nothing. Reporting the submitted count there would claim
    "enriched 70,580" and compute a coverage gap of zero — the absence
    made to look like success, which is this project's most expensive
    recurring bug.

    execute_values pages internally and leaves cur.rowcount reflecting
    only the final page, so getting a true total means paging here and
    summing.
    """
    if not rows:
        return 0

    # A tuple whose arity does not match the template would silently shift
    # values into the wrong columns. Fail loudly instead.
    if template is not None:
        expected = template.count("%s")
        bad = next((r for r in rows if len(r) != expected), None)
        if bad is not None:
            raise ValueError(
                f"row arity {len(bad)} does not match template arity {expected}"
            )

    from psycopg2.extras import execute_values

    raw = get_engine().raw_connection()
    try:
        affected = 0
        with raw.cursor() as cur:
            if count_affected:
                for i in range(0, len(rows), page_size):
                    page = rows[i:i + page_size]
                    execute_values(cur, sql_with_values, page,
                                   template=template, page_size=len(page))
                    affected += max(cur.rowcount, 0)
            else:
                execute_values(cur, sql_with_values, rows,
                               template=template, page_size=page_size)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return affected if count_affected else len(rows)


def upsert_satellites(satellites: Iterable[Mapping[str, Any]]) -> int:
    """
    Upsert satellite catalog rows keyed on norad_id.

    Each item needs at least norad_id and name. Any other column from
    _SATELLITE_COLUMNS is optional — missing fields are left NULL on
    insert and preserved (not overwritten with NULL) on update, via
    COALESCE. This lets different sources enrich the same row over time
    (e.g. TLE fetch fills orbit_regime/mean_motion/tle_line1/2; a later
    catalog-enrichment step fills operator/manufacturer without erasing
    the TLE fields already written).

    `source` should be 'celestrak' or 'spacetrack' (matches column default
    'spacetrack' — pass explicitly rather than relying on the default when
    writing from CelesTrak data).
    """
    rows = [{c: s.get(c) for c in _SATELLITE_COLUMNS} for s in satellites]
    if not rows:
        return 0
    for r in rows:
        if r.get("norad_id") is None:
            raise ValueError(f"satellite row missing norad_id: {r}")

    tuples = [tuple(r.get(c) for c in _SATELLITE_COLUMNS) for r in rows]
    written = _bulk_upsert(_UPSERT_SATELLITE_SQL, tuples,
                           template=_SATELLITE_VALUES_TEMPLATE)
    logger.info("Upserted %d satellite rows.", written)
    return written


# ══════════════════════════════════════════════════════════════════════════════
#  Catalogue enrichment (Phase 2)
# ══════════════════════════════════════════════════════════════════════════════
#
# A SECOND writer for `satellites`, deliberately separate from
# upsert_satellites. The 2-hourly CelesTrak fetch owns the orbital columns;
# enrichment owns attribution and provenance. They meet at COALESCE, so
# neither erases the other's work.
#
# This one UPDATES ONLY. It will not create a satellite.
#
# That is the important constraint. SATCAT holds 70,580 records against
# this catalogue's ~18,000, and 35,542 of them are decayed objects that
# re-entered years ago. An INSERT here would quadruple the table on a
# 500 MB tier as a side effect of an enrichment pass — a scope decision
# disguised as a data-quality one. Expanding the catalogue is a separate,
# deliberate operation; enriching what we already track is this one.
#
# Each value carries an explicit cast. execute_values sends VALUES tuples
# untyped, and Postgres will not silently coerce text into DATE, REAL or
# TIMESTAMPTZ inside an UPDATE ... FROM — it errors, which is better than
# guessing, but only if the casts are here to begin with.

_ATTRIBUTION_COLUMNS = [
    ("norad_id",          "int"),
    ("object_type",       "text"),
    ("status",            "text"),
    ("owner_code",        "text"),
    ("country_code",      "text"),
    ("rcs_size",          "text"),
    ("launch_date",       "date"),
    ("launch_site",       "text"),
    ("period_min",        "real"),
    ("inclination_deg",   "real"),
    ("apogee_km",         "real"),
    ("perigee_km",        "real"),
    # Added 2026-09-12 for GCAT (seed_gcat.py). SATCAT fills neither:
    #
    #   operator   SATCAT's OWNER is a country-level code, which goes to
    #              owner_code. GCAT's Owner is the operating organisation,
    #              resolved to a name through orgs.tsv.
    #   orbit_type SATCAT's ORBIT_TYPE is a disposition (ORB/IMP/LAN/DOC),
    #              deliberately not mapped in seed_satcat.py because it is
    #              not geometry. GCAT's OpOrbit is geometry - 24 values,
    #              LEO/S, GEO/ID, GTO - which is what this column is for.
    #
    # NOT orbit_regime: that belongs to the 2-hourly fetch, and
    # test_enrichment_does_not_touch_the_orbital_columns enforces it.
    ("operator",          "text"),
    ("orbit_type",        "text"),
    # Added 2026-09-12 (008_deployment_date.sql). When the object began
    # flying independently, which for anything released from a space
    # station is not when launch_date says. GCAT's LDate, via seed_gcat.
    ("deployment_date",   "date"),
    ("data_source",       "text"),
    ("match_method",      "text"),
    ("source_confidence", "real"),
    ("matched_at",        "timestamptz"),
]

#: Descriptive columns use COALESCE so a source that omits a field cannot
#: blank one another source already filled.
_ATTRIBUTION_DESCRIPTIVE = [
    c for c, _ in _ATTRIBUTION_COLUMNS
    if c not in ("norad_id", "data_source", "match_method",
                 "source_confidence", "matched_at")
]

#: Provenance is assigned outright. It describes THIS pass, so preserving
#: an older value would misreport where the row's attribution came from.
_ATTRIBUTION_PROVENANCE = ["data_source", "match_method",
                           "source_confidence", "matched_at"]

_UPDATE_ATTRIBUTION_SQL = f"""
    UPDATE satellites AS s SET
        {", ".join(f"{c} = COALESCE(v.{c}, s.{c})"
                   for c in _ATTRIBUTION_DESCRIPTIVE)},
        {", ".join(f"{c} = v.{c}" for c in _ATTRIBUTION_PROVENANCE)},
        last_updated = now()
    FROM (VALUES %s) AS v({", ".join(c for c, _ in _ATTRIBUTION_COLUMNS)})
    WHERE s.norad_id = v.norad_id
"""

_ATTRIBUTION_VALUES_TEMPLATE = (
    "(" + ", ".join(f"%s::{t}" for _, t in _ATTRIBUTION_COLUMNS) + ")"
)


def upsert_satellite_attribution(
        rows: Iterable[Mapping[str, Any]]) -> int:
    """
    Write catalogue attribution onto satellites that already exist.

    Returns the number of rows actually updated — which is NOT the number
    passed in. A SATCAT record for an object this catalogue does not track
    matches nothing and updates nothing, and the difference between the
    two numbers is the coverage gap, worth reporting rather than hiding.

    Every row must carry provenance. A descriptive value with no
    data_source cannot be re-examined or rolled back with its source,
    which is the failure 004_catalog_provenance.sql exists to prevent, so
    it is rejected here rather than written and detected later by
    check_catalog.py.
    """
    prepared = []
    for r in rows:
        if r.get("norad_id") is None:
            raise ValueError(f"attribution row missing norad_id: {r}")
        if not r.get("data_source"):
            raise ValueError(
                f"attribution row for norad {r['norad_id']} has no "
                f"data_source. Every enriched row must say where it came "
                f"from — see 004_catalog_provenance.sql.")
        prepared.append(tuple(r.get(c) for c, _ in _ATTRIBUTION_COLUMNS))

    if not prepared:
        return 0

    written = _bulk_upsert(_UPDATE_ATTRIBUTION_SQL, prepared,
                           template=_ATTRIBUTION_VALUES_TEMPLATE,
                           count_affected=True)
    logger.info("Enriched %d satellite rows.", written)
    return written


# ══════════════════════════════════════════════════════════════════════════════
#  Catalogue events
# ══════════════════════════════════════════════════════════════════════════════
#
# What check_new_objects.py noticed, made durable. See
# 006_catalog_events.sql for why this is a table rather than an alert.
#
# The upsert keys on (event_type, event_key) because the monitor sweeps a
# rolling window daily and re-detects the same launches for weeks. Without
# that, a month of runs turns one launch into thirty rows and every
# dashboard count is wrong.

#: Objects listed per event. object_count carries the true total; this is
#: a sample so an ingestion change covering 587 fragments does not write a
#: 587-element array on every one of thirty daily re-detections.
MAX_NORAD_SAMPLE = 200

_CATALOG_EVENT_COLUMNS = [
    ("event_type",   "text"),
    ("event_key",    "text"),
    ("launch_key",   "text"),
    ("launch_date",  "date"),
    ("object_count", "int"),
    ("object_types", "text"),
    ("norad_ids",    "integer[]"),
    ("first_seen",   "date"),
    ("notable",      "boolean"),
    ("details",      "jsonb"),
]

_UPSERT_CATALOG_EVENT_SQL = f"""
    INSERT INTO catalog_events (
        {", ".join(c for c, _ in _CATALOG_EVENT_COLUMNS)}
    ) VALUES %s
    ON CONFLICT (event_type, event_key) DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}"
                   for c, _ in _CATALOG_EVENT_COLUMNS
                   if c not in ("event_type", "event_key"))},
        updated_at = now()
"""

_CATALOG_EVENT_VALUES_TEMPLATE = (
    "(" + ", ".join(f"%s::{t}" for _, t in _CATALOG_EVENT_COLUMNS) + ")"
)


def upsert_catalog_events(events: Iterable[Mapping[str, Any]]) -> int:
    """
    Record what the catalogue noticed. Re-running is safe.

    `details` may be a dict; it is serialised here so callers do not each
    have to remember to. `norad_ids` is truncated to MAX_NORAD_SAMPLE —
    `object_count` is the number that means something.
    """
    import json

    rows = []
    for e in events:
        if not e.get("event_type") or not e.get("event_key"):
            raise ValueError(
                f"catalog event needs event_type and event_key: {e}")
        row = dict(e)
        ids = row.get("norad_ids") or []
        row["norad_ids"] = [int(n) for n in list(ids)[:MAX_NORAD_SAMPLE]]
        row["object_count"] = int(row.get("object_count") or 0)
        row["notable"] = bool(row.get("notable", False))
        details = row.get("details")
        row["details"] = (json.dumps(details)
                          if isinstance(details, (dict, list)) else details)
        rows.append(tuple(row.get(c) for c, _ in _CATALOG_EVENT_COLUMNS))

    if not rows:
        return 0

    written = _bulk_upsert(_UPSERT_CATALOG_EVENT_SQL, rows,
                           template=_CATALOG_EVENT_VALUES_TEMPLATE)
    logger.info("Recorded %d catalogue event(s).", written)
    return written


_INSERT_TLE_HISTORY_SQL = """
    INSERT INTO tle_history (norad_id, line1, line2, epoch, source)
    VALUES %s
    ON CONFLICT (norad_id, epoch) DO NOTHING
"""
_TLE_HISTORY_COLUMNS = ["norad_id", "line1", "line2", "epoch", "source"]


def _as_datetime(value: Any) -> Any:
    """
    Parse CelesTrak's ISO EPOCH string into an aware UTC datetime; pass
    anything else through.

    THE TIMEZONE IS NOT OPTIONAL HERE
    =================================
    CelesTrak's OMM JSON writes EPOCH as '2026-09-04T11:00:00.000000' -
    no 'Z', no offset. `datetime.fromisoformat` therefore returns a NAIVE
    datetime, and comparing that against the aware horizon in
    insert_tle_history raises:

        TypeError: can't compare offset-naive and offset-aware datetimes

    which is what broke every TLE write from 2026-09-01 to 09-04. The
    fetch step kept logging success; only write_db failed, so satellites
    updated while tle_history sat frozen at 346,171 rows.

    OMM epochs are UTC by definition (CCSDS 502.0-B-2), so a missing
    designator means UTC, not unknown. Attach it rather than leaving the
    value naive and hoping the server guesses right.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value                      # let the server judge it
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _dedupe_key(norad_id: Any, epoch: Any) -> tuple:
    """
    Identity of an element set for within-batch deduplication.

    THE PROBLEM. One fetch run pulls five CelesTrak groups and writes
    them as a single batch. An object listed in two of those groups
    arrives twice, and the two copies carry epochs that differ in the
    last representable digit of the TLE format - observed on object
    69235, two rows 864 microseconds apart. UNIQUE (norad_id, epoch)
    cannot merge them because the timestamps really are different, so
    both are stored.

    WHY NOT ROUND THE STORED VALUE. Tried first, and it fails on exactly
    the case that motivated it: 376672 and 375808 microseconds round to
    different milliseconds because they straddle a boundary. Any
    fixed-width bucket has boundary pairs. Coarsening far enough to be
    safe (whole seconds) would also mean storing an epoch that no longer
    matches the element set's own lines, which is a worse trade than the
    duplicate rows.

    So: full precision is stored, and the key used to spot duplicates
    *within a batch* is truncated to the second. Genuine element sets for
    one satellite are hours apart, never one second, so this cannot merge
    two real observations - and duplicates arriving in later runs are
    still caught by the unique constraint, because the same source data
    renders identically each time.
    """
    if isinstance(epoch, datetime):
        epoch = epoch.replace(microsecond=0)
    return (norad_id, epoch)


def insert_tle_history(records: Iterable[Mapping[str, Any]]) -> int:
    """
    Append raw TLE records for archival / confidence-scoring reuse.

    Each item: norad_id, line1, line2, epoch (tz-aware datetime), source
    ('celestrak' | 'spacetrack', defaults to 'spacetrack' if omitted to
    match the column default). The unique constraint is (norad_id, epoch)
    only — NOT source — so if both sources ever report the same epoch for
    the same satellite, the second insert is silently skipped rather than
    erroring. Requires the satellite to already exist (FK).
    """
    horizon = datetime.now(timezone.utc) + timedelta(days=1)
    rows, future, dupes = [], 0, 0
    seen: set = set()
    for r in records:
        epoch = _as_datetime(r["epoch"])

        # CelesTrak occasionally emits element sets epoched days ahead.
        # 2026-09-01: rows existed at 2026-09-07, six days out. The
        # propagator already refuses to propagate anything more than a
        # day future-dated, so these were never usable - only stored.
        if isinstance(epoch, datetime) and epoch > horizon:
            future += 1
            continue

        key = _dedupe_key(r["norad_id"], epoch)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)

        rows.append({
            "norad_id": r["norad_id"],
            "line1": r["line1"],
            "line2": r["line2"],
            "epoch": epoch,
            "source": r.get("source", "spacetrack"),
        })
    if future:
        logger.warning("Skipped %d element set(s) epoched more than a day "
                       "in the future.", future)
    if dupes:
        logger.info("Collapsed %d element set(s) listed in more than one "
                    "CelesTrak group.", dupes)
    if not rows:
        return 0
    tuples = [tuple(r.get(c) for c in _TLE_HISTORY_COLUMNS) for r in rows]

    # count_affected, because this is ON CONFLICT DO NOTHING: rows that
    # collide with the (norad_id, epoch) unique constraint are discarded
    # by the database and were previously counted as written anyway.
    #
    # The old log line said "Inserted up to N rows (duplicates skipped)"
    # and returned N — it knew it did not know, and reported the
    # optimistic figure regardless.
    #
    # The blast radius was smaller than it looks, and worth stating
    # accurately: fetcher.py discarded this return value, and the
    # `records_processed` it logs is the *satellites* upsert count. So
    # the inflated figure only ever reached stdout, and the 2026-09-01
    # size investigation was unaffected — check_tle_history.py counts the
    # table directly.
    #
    # What was actually missing is that archival volume was never
    # recorded at all. fetcher.py now logs this count as its own step.
    written = _bulk_upsert(_INSERT_TLE_HISTORY_SQL, tuples,
                           count_affected=True)
    already = len(rows) - written
    if already:
        logger.info("Inserted %d tle_history row(s); %d already held.",
                    written, already)
    else:
        logger.info("Inserted %d tle_history row(s).", written)
    return written


# =====================================================
# ORBITAL POSITIONS (time series, pruned to 48h)
# =====================================================

_UPSERT_POSITION_SQL = text("""
    INSERT INTO orbital_positions (
        norad_id, timestamp, latitude, longitude, altitude_km,
        velocity_km_s, azimuth_deg, elevation_deg, range_km
    ) VALUES (
        :norad_id, :timestamp, :latitude, :longitude, :altitude_km,
        :velocity_km_s, :azimuth_deg, :elevation_deg, :range_km
    )
    ON CONFLICT (norad_id, timestamp) DO UPDATE SET
        latitude      = EXCLUDED.latitude,
        longitude     = EXCLUDED.longitude,
        altitude_km   = EXCLUDED.altitude_km,
        velocity_km_s = EXCLUDED.velocity_km_s,
        azimuth_deg   = EXCLUDED.azimuth_deg,
        elevation_deg = EXCLUDED.elevation_deg,
        range_km      = EXCLUDED.range_km
""")


def upsert_orbital_positions(positions: Iterable[Mapping[str, Any]]) -> int:
    """
    Write a batch of propagated positions.

    Each item: norad_id, timestamp (tz-aware datetime), latitude,
    longitude, altitude_km (all required); velocity_km_s, azimuth_deg,
    elevation_deg, range_km (optional — the latter three only make sense
    if computed relative to a specific observer, so leave them None for
    plain sub-satellite-point propagation and populate them if this batch
    was computed relative to a sensor). Requires the satellite to already
    exist (FK). Call prune_old_positions() periodically to keep this
    table bounded to the last 48 hours.
    """
    rows = [
        (
            p["norad_id"],
            p["timestamp"],
            p["latitude"],
            p["longitude"],
            p["altitude_km"],
            p.get("velocity_km_s"),
            p.get("azimuth_deg"),
            p.get("elevation_deg"),
            p.get("range_km"),
        )
        for p in positions
    ]
    if not rows:
        return 0

    written = _bulk_upsert(
        """
        INSERT INTO orbital_positions (
            norad_id, timestamp, latitude, longitude, altitude_km,
            velocity_km_s, azimuth_deg, elevation_deg, range_km
        ) VALUES %s
        ON CONFLICT (norad_id, timestamp) DO UPDATE SET
            latitude      = EXCLUDED.latitude,
            longitude     = EXCLUDED.longitude,
            altitude_km   = EXCLUDED.altitude_km,
            velocity_km_s = EXCLUDED.velocity_km_s,
            azimuth_deg   = EXCLUDED.azimuth_deg,
            elevation_deg = EXCLUDED.elevation_deg,
            range_km      = EXCLUDED.range_km
        """,
        rows,
    )
    logger.info("Upserted %d orbital_positions rows.", written)
    return written


# =====================================================
# ARCHIVE WATERMARK
# =====================================================

# Tables whose history is copied to local disk by archive_to_local.py
# before being deleted from here. Must match that script's TABLES.
_ARCHIVED_TABLES = {"visibility_windows", "orbital_positions", "tle_history"}



def _archive_cutoff(table: str, soft_cutoff: datetime) -> "tuple[datetime | None, str]":
    """
    How far back it is safe to delete from `table`, and why.

    Returns (cutoff, reason). A cutoff of None means delete nothing.

    The prune used to be a function of the clock alone. Since 2026-09-10
    this database is the serving layer and `D:` is the warehouse, so the
    clock is no longer sufficient: the prunes run in GitHub Actions on a
    fixed schedule while the archive runs on a workstation that might be
    off. Fixed-clock prune plus intermittent archive loses data silently,
    and orbital_positions gives a two-day margin before it does.

    So:

        cutoff = LEAST(now() - retention, archived_through)

    with three outcomes, all of them deliberate:

      archive current   the watermark is ahead of the clock cutoff, the
                        clock wins, and this behaves exactly as it did
                        before the watermark existed.

      archive behind    the watermark wins. Less is deleted, the table
                        grows, and nothing is lost. This is the failure
                        mode we want.

      archive stalled   past max_retention_days the backstop deletes
                        unarchived rows anyway, because an unbounded
                        table exhausts a 500 MB tier in days and stops
                        every pipeline at once. It is logged at ERROR
                        and recorded as a catalog_events row, because a
                        silent path that destroys data is the thing this
                        whole design exists to prevent.

    A table with no watermark row at all is treated as unmanaged and
    prunes on the clock, so this function cannot break a table that was
    never part of the archive scheme.
    """
    if table not in _ARCHIVED_TABLES:
        return soft_cutoff, "clock (table is not archived)"

    with _tx() as conn:
        row = conn.execute(
            text("SELECT archived_through, max_retention_days "
                 "FROM archive_watermark WHERE table_name = :t"),
            {"t": table},
        ).fetchone()

    if row is None:
        # 007 has not been applied, or someone deleted the row. Falling
        # back to the clock would silently restore the old unsafe
        # behaviour, so refuse instead: a table that grows for a day is
        # recoverable, a deleted element set is not.
        logger.error(
            "No archive_watermark row for %s. Refusing to prune. Apply "
            "007_archive_watermark.sql, or remove %s from "
            "_ARCHIVED_TABLES if it is no longer archived.", table, table)
        return None, "refused (no watermark row)"

    watermark, max_days = row

    if watermark is None:
        # Nothing archived yet. Deleting now would destroy rows that have
        # never been copied anywhere.
        if max_days is None:
            logger.warning(
                "%s has never been archived and has no backstop. Pruning "
                "nothing; the table will grow until archive_to_local.py "
                "runs.", table)
            return None, "nothing archived yet, no backstop"
        hard = datetime.now(timezone.utc) - timedelta(days=max_days)
        logger.error(
            "%s has never been archived. Backstop deleting rows older "
            "than %d days that are on no disk anywhere.", table, max_days)
        return hard, f"BACKSTOP ({max_days}d, nothing archived)"

    if watermark.tzinfo is None:
        watermark = watermark.replace(tzinfo=timezone.utc)

    cutoff = min(soft_cutoff, watermark)
    reason = "clock" if cutoff == soft_cutoff else "archive watermark"

    if max_days is not None:
        hard = datetime.now(timezone.utc) - timedelta(days=max_days)
        if hard > cutoff:
            logger.error(
                "ARCHIVE BACKSTOP on %s: watermark is at %s but the %d-day "
                "backstop forces deletion to %s. Rows between those points "
                "are on no disk anywhere and are being destroyed. The "
                "archive has not run - check the SatelliteArchive scheduled "
                "task and D:\\Databases\\satellite\\archive\\archive.log.",
                table, watermark, max_days, hard)
            _record_backstop_event(table, watermark, hard, max_days)
            return hard, f"BACKSTOP ({max_days}d)"

    return cutoff, reason


def _record_backstop_event(table: str, watermark, hard, max_days: int) -> None:
    """
    Put the backstop in the monitor's digest.

    Logging alone is not enough: these prunes run inside GitHub Actions,
    and nobody reads a green job's logs. catalog_events is what the daily
    digest reads, and event_type is outside ROUTINE_TYPES so
    report_events.py treats it as newsworthy and the issue gets opened.

    Failure to record must never stop the prune - the tier is full, which
    is why we are here.
    """
    try:
        with _tx() as conn:
            conn.execute(text("""
                INSERT INTO catalog_events
                    (event_type, event_key, object_count, notable, details)
                VALUES
                    ('archive_backstop', :k, 0, true, :d)
                ON CONFLICT (event_type, event_key) DO UPDATE
                   SET details = EXCLUDED.details, updated_at = NOW()
            """), {
                "k": f"{table}:{datetime.now(timezone.utc).date()}",
                "d": json.dumps({
                    "table": table,
                    "archived_through": str(watermark),
                    "forced_cutoff": str(hard),
                    "max_retention_days": max_days,
                    "message": "Unarchived rows were deleted to keep the "
                               "tier from filling. The archive has not run.",
                }),
            })
    except Exception as exc:                       # noqa: BLE001
        logger.error("Could not record archive_backstop event: %s", exc)


def prune_old_positions(hours: int = 48) -> int:
    """
    Delete orbital_positions older than `hours`. Returns rows deleted.

    The schema already ships a matching SQL function
    (prune_old_positions() in 001_core_schema.sql, hardcoded to 48h) that
    can be run via pg_cron or a scheduled Actions step instead of this
    Python version — use whichever is more convenient for your scheduler.
    This version exists so a single Python-side ingestion run can prune
    right after writing new positions without a second round-trip to call
    the SQL function.
    """
    soft = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff, reason = _archive_cutoff("orbital_positions", soft)
    if cutoff is None:
        logger.info("Pruned 0 orbital_positions rows: %s.", reason)
        return 0

    with _tx() as conn:
        result = conn.execute(
            text("DELETE FROM orbital_positions WHERE timestamp < :cutoff"),
            {"cutoff": cutoff},
        )
        deleted = result.rowcount or 0
    logger.info("Pruned %d orbital_positions rows older than %s (%s; "
                "asked for %dh).", deleted, cutoff, reason, hours)
    return deleted


# =====================================================
# SENSORS
# =====================================================

def get_sensor_id(short_name: str) -> int:
    """
    Look up a sensor's integer id by its short_name (e.g. 'FPS85',
    'GEODSS_SOC', 'MILLSTONE' — see the seed rows in 001_core_schema.sql).
    visibility_windows.sensor_id is an FK to sensors.id, not short_name,
    so this lookup is needed before writing visibility windows.
    """
    with _tx() as conn:
        row = conn.execute(
            text("SELECT id FROM sensors WHERE short_name = :short_name"),
            {"short_name": short_name},
        ).fetchone()
    if row is None:
        raise ValueError(
            f"No sensor found with short_name={short_name!r}. "
            f"Check sensors.short_name in Supabase, or seed it via "
            f"001_core_schema.sql's INSERT INTO sensors block."
        )
    return row[0]


# =====================================================
# VISIBILITY WINDOWS
# =====================================================

#: Tuple order for the batched insert below. The payload dicts are converted
#: to tuples using exactly this list, so the two cannot drift apart.
_VISIBILITY_COLUMNS = [
    "norad_id", "sensor_id", "analysis_date", "window_start", "window_end",
    "hour_bin", "max_elevation", "max_azimuth", "min_range_km",
    "orbit_regime", "confidence_score",
]

_UPSERT_VISIBILITY_SQL = f"""
    INSERT INTO visibility_windows (
        {", ".join(_VISIBILITY_COLUMNS)}
    ) VALUES %s
    ON CONFLICT (norad_id, sensor_id, window_start) DO UPDATE SET
        window_end       = EXCLUDED.window_end,
        hour_bin          = EXCLUDED.hour_bin,
        max_elevation     = EXCLUDED.max_elevation,
        max_azimuth       = EXCLUDED.max_azimuth,
        min_range_km      = EXCLUDED.min_range_km,
        orbit_regime      = EXCLUDED.orbit_regime,
        confidence_score  = COALESCE(EXCLUDED.confidence_score, visibility_windows.confidence_score)
"""


def insert_visibility_windows(
    rows: Iterable[Mapping[str, Any]],
    sensor_short_name: str,
    analysis_date: date,
    bin_size_hours: int = 1,
) -> int:
    """
    Write visibility results from a Satellite Visibility Tool run
    (main.py's `results` list — the same rows export_utils.export_to_excel
    currently writes to visible_satellites.xlsx).

    Accepts the export dict keys directly: "Hour Window", "Target Name",
    "Target Orbit", "Target NORAD", "Elevation (deg)", "Azimuth (deg)",
    "Range (km)" — so --headless mode can pass main.py's results straight
    through. Optional "confidence_score" key (from accuracy_model.py's
    score_orbital_stability() output) is included if present.

    NOTE ON max_elevation / max_azimuth / min_range_km: main.py currently
    records the elevation/azimuth/range at the *first* visible instant in
    each hour bin, not a true max/min across the bin (see main.py's
    `first_idx = idxs[0]`). Those values are written into these columns
    as-is. If you want a true max elevation / min range per bin later,
    that's a change to main.py's aggregation, not to this writer.

    hour_bin / window_start / window_end are derived from "Hour Window"'s
    position in sequence + analysis_date + bin_size_hours (must match
    config.py's BIN_SIZE_HOURS for the run that produced these rows),
    rather than parsed from the label string, to avoid midnight-rollover
    parsing edge cases.

    Requires the satellite to already exist (FK) — call upsert_satellites()
    first for every norad_id appearing in `rows`.
    """
    sensor_id = get_sensor_id(sensor_short_name)

    # "Hour Window" labels look like "0600-0700Z" (see main.py bin_labels).
    # Extract just the starting hour to compute hour_bin / window_start,
    # rather than trusting the full string format.
    def _hour_bin_from_label(label: str) -> int:
        start_str = label.split("-")[0]
        return int(start_str[:2])

    payload = []
    for r in rows:
        hour_bin = _hour_bin_from_label(r["Hour Window"])
        window_start = datetime.combine(analysis_date, datetime.min.time(), tzinfo=timezone.utc) \
            + timedelta(hours=hour_bin)
        window_end = window_start + timedelta(hours=bin_size_hours)

        payload.append({
            "norad_id": r["Target NORAD"],
            "sensor_id": sensor_id,
            "analysis_date": analysis_date,
            "window_start": window_start,
            "window_end": window_end,
            "hour_bin": hour_bin,
            "max_elevation": r.get("Elevation (deg)"),
            "max_azimuth": r.get("Azimuth (deg)"),
            "min_range_km": r.get("Range (km)"),
            "orbit_regime": r.get("Target Orbit"),
            "confidence_score": r.get("confidence_score"),
        })

    if not payload:
        return 0

    # Batched, not executemany. Visibility runs produce more rows than the
    # TLE fetch does - satellites x sensors x hour bins - and the per-row
    # path silently discarded a week of TLE writes when it hit the workflow
    # timeout (2026-08-26..31). Converted before this table's first real
    # run rather than after.
    tuples = [tuple(row[c] for c in _VISIBILITY_COLUMNS) for row in payload]
    written = _bulk_upsert(_UPSERT_VISIBILITY_SQL, tuples)
    logger.info(
        "Upserted %d visibility_windows rows for sensor=%s on %s.",
        written, sensor_short_name, analysis_date,
    )
    return written


def prune_old_tle_history(days: int = 14) -> int:
    """
    Delete tle_history rows epoched more than `days` ago.

    Measured 2026-09-01: 1,574,305 rows, 515 MB - 86% of a 597 MB
    database against a 500 MB tier. The rows are legitimate (1-3 new
    element sets per satellite per day, not repeated fetches of the same
    one), so this is a budget decision rather than a bug fix: at ~30,000
    new rows a day the table grows about 10 MB daily and nothing bounded
    it.

    14 days keeps 113 MB and reclaims 78%. Widen it whenever the tier
    allows - the confidence-scoring work this archive exists for wants
    more history, not less.

    NOTE: a DELETE marks rows dead but does not return their space to the
    filesystem, and Supabase bills on what is on disk. After a large
    prune run `VACUUM FULL tle_history;` (see cleanup_tle_history.py) or
    the reported size will not move.
    """
    soft = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff, reason = _archive_cutoff("tle_history", soft)
    if cutoff is None:
        logger.info("Pruned 0 tle_history rows: %s.", reason)
        return 0

    with _tx() as conn:
        result = conn.execute(
            text("DELETE FROM tle_history WHERE epoch < :cutoff"),
            {"cutoff": cutoff},
        )
        deleted = result.rowcount or 0
    logger.info("Pruned %d tle_history rows older than %s (%s; asked for "
                "%d days).", deleted, cutoff, reason, days)
    return deleted


def prune_old_visibility_windows(days: int = 7) -> int:
    """
    Delete visibility_windows for analysis dates older than `days`.

    WHY THIS EXISTS
    ---------------
    Measured on 2026-09-01 against the live 18,044-object catalogue:
    FPS85 43,621 windows/day, GEODSS_SOC 75,084, MILLSTONE 103,083 —
    221,788 rows every day. Unlike orbital_positions, nothing here
    self-limits: the upsert key is (norad_id, sensor_id, window_start),
    so re-running a day updates in place but each new day accumulates.
    Left unpruned this table alone would exhaust the 500 MB tier in
    roughly a fortnight.

    Prunes on analysis_date rather than window_start so a whole run
    leaves together and no day is left half-deleted.
    """
    soft = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff, reason = _archive_cutoff("visibility_windows", soft)
    if cutoff is None:
        logger.info("Pruned 0 visibility_windows rows: %s.", reason)
        return 0

    # analysis_date is a DATE and the watermark is that date at 00:00 UTC,
    # so .date() here keeps a whole run leaving together - the property
    # the original prune was written for.
    with _tx() as conn:
        result = conn.execute(
            text("DELETE FROM visibility_windows WHERE analysis_date < :cutoff"),
            {"cutoff": cutoff.date()},
        )
        deleted = result.rowcount or 0
    logger.info("Pruned %d visibility_windows rows older than %s (%s; asked "
                "for %d days).", deleted, cutoff.date(), reason, days)
    return deleted


def table_size_bytes(table_name: str) -> "int | None":
    """
    On-disk size of a table including its indexes and TOAST, or None if
    the server will not answer.
    """
    try:
        with get_engine().connect() as conn:
            return int(conn.execute(
                text("SELECT pg_total_relation_size(:t)"),
                {"t": table_name},
            ).scalar())
    except Exception as exc:                       # not worth failing a run over
        logger.debug("Could not read size of %s: %s", table_name, exc)
        return None


_DB_SIZES_SQL = """
    SELECT c.relname, pg_total_relation_size(c.oid) AS bytes
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'r'
     ORDER BY bytes DESC
"""


def database_sizes() -> "list[tuple[str, int]]":
    """
    Every public table with its total on-disk size, largest first.

    Retention windows on this project are a budget problem, not a policy
    one: the tier is 500 MB and three tables compete for it. Sizing any
    one of them from an estimate is how you find out you were wrong by
    running out of room. This reports the whole budget so a retention
    decision can be made against measured bytes.
    """
    try:
        with get_engine().connect() as conn:
            return [(r[0], int(r[1]))
                    for r in conn.execute(text(_DB_SIZES_SQL)).fetchall()]
    except Exception as exc:
        logger.debug("Could not read table sizes: %s", exc)
        return []


# =====================================================
# IMAGERY SCENES (from ingest.py)
# =====================================================

_IMAGERY_COLUMNS = [
    "scene_id", "filename", "sensor", "satellite", "date_acquired", "tile",
    "crs", "bounds_west", "bounds_east", "bounds_south", "bounds_north",
    "cloud_cover_pct", "bands", "resolution_m", "shape_rows", "shape_cols",
    "file_size_mb", "raw_path", "processed_path", "status", "processing_log",
]

_UPSERT_IMAGERY_SQL = text(f"""
    INSERT INTO imagery_scenes (
        {", ".join(_IMAGERY_COLUMNS)}
    ) VALUES (
        {", ".join(f":{c}" for c in _IMAGERY_COLUMNS)}
    )
    ON CONFLICT (scene_id) DO UPDATE SET
        {", ".join(f"{c} = EXCLUDED.{c}" for c in _IMAGERY_COLUMNS if c != "scene_id")}
""")


def upsert_imagery_scene(scene: Mapping[str, Any]) -> None:
    """
    Upsert one imagery scene record, keyed on scene_id (e.g.
    "S2C_20260621_T13SED", matching the column comment in
    001_core_schema.sql). Required: scene_id, filename, sensor,
    date_acquired. Everything else in _IMAGERY_COLUMNS is optional.

    `bands` is JSON-encoded automatically if passed as a Python list
    (e.g. ["B02", "B03", "B04", "B08"]).
    """
    payload = {c: scene.get(c) for c in _IMAGERY_COLUMNS}
    for required in ("scene_id", "filename", "sensor", "date_acquired"):
        if payload.get(required) is None:
            raise ValueError(f"imagery scene missing required field: {required}")

    if isinstance(payload.get("bands"), (list, dict)):
        payload["bands"] = json.dumps(payload["bands"])

    payload.setdefault("status", "processed")

    # Deliberately not batched: this writes ONE scene per call (imagery is
    # ingested a scene at a time), so there is no executemany here to be
    # slow. Do not "fix" this to match the other writers.
    with _tx() as conn:
        conn.execute(_UPSERT_IMAGERY_SQL, payload)
    logger.info("Upserted imagery_scenes row for scene_id=%s.", payload["scene_id"])


# =====================================================
# INGESTION LOG (pipeline audit)
# =====================================================
# Schema note: ingestion_log has no started_at/finished_at columns to
# update in place — it's an append-only, one-row-per-step log, with
# run_id (UUID) grouping the steps of a single pipeline execution
# together. Generate one run_id per pipeline invocation and pass it to
# every log_step() call for that run.

_INSERT_LOG_SQL = text("""
    INSERT INTO ingestion_log (
        run_id, pipeline, step, status, message,
        records_processed, duration_s, source, github_run_id
    ) VALUES (
        :run_id, :pipeline, :step, :status, :message,
        :records_processed, :duration_s, :source, :github_run_id
    )
""")


def new_run_id() -> str:
    """Generate a run_id (UUID string) to group all log_step() calls for one pipeline execution."""
    return str(uuid.uuid4())


def log_step(
    run_id: str,
    pipeline: str,
    step: str,
    status: str,
    message: Optional[str] = None,
    records_processed: int = 0,
    duration_s: Optional[float] = None,
    source: Optional[str] = None,
    github_run_id: Optional[str] = None,
) -> None:
    """
    Log one pipeline step. status: 'success' | 'failed' | 'skipped' | 'partial'
    (matches the column comment in 001_core_schema.sql). pipeline: e.g.
    'tle_fetch', 'visibility', 'imagery'. step: e.g. 'validate', 'convert',
    'index', 'archive' — or any label meaningful to that pipeline.

    github_run_id defaults to the GITHUB_RUN_ID environment variable
    (automatically set inside GitHub Actions) if not passed explicitly,
    so Actions-triggered runs are traceable back to the workflow run
    without extra wiring at every call site.
    """
    with _tx() as conn:
        conn.execute(
            _INSERT_LOG_SQL,
            {
                "run_id": run_id,
                "pipeline": pipeline,
                "step": step,
                "status": status,
                "message": message,
                "records_processed": records_processed,
                "duration_s": duration_s,
                "source": source,
                "github_run_id": github_run_id or os.environ.get("GITHUB_RUN_ID"),
            },
        )
    logger.info("Logged step run_id=%s pipeline=%s step=%s status=%s.", run_id, pipeline, step, status)


@contextmanager
def ingestion_step(
    run_id: str,
    pipeline: str,
    step: str,
    source: Optional[str] = None,
    github_run_id: Optional[str] = None,
):
    """
    Context manager that times a step and logs it automatically —
    'success' with duration_s if the block completes, 'failed' with the
    exception message if it raises (the exception is re-raised after
    logging, it is not swallowed).

        run_id = new_run_id()
        with ingestion_step(run_id, "tle_fetch", "validate"):
            n = upsert_satellites(rows)
    """
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        log_step(
            run_id, pipeline, step, status="failed",
            message=str(exc), duration_s=time.monotonic() - start,
            source=source, github_run_id=github_run_id,
        )
        raise
    else:
        log_step(
            run_id, pipeline, step, status="success",
            duration_s=time.monotonic() - start,
            source=source, github_run_id=github_run_id,
        )


# =====================================================
# CLI — quick connectivity check
# =====================================================

if __name__ == "__main__":
    import sys

    ok = check_connection()
    sys.exit(0 if ok else 1)
