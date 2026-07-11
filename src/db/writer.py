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

_UPSERT_SATELLITE_SQL = text(f"""
    INSERT INTO satellites (
        {", ".join(_SATELLITE_COLUMNS)}, last_updated
    ) VALUES (
        {", ".join(f":{c}" for c in _SATELLITE_COLUMNS)}, now()
    )
    ON CONFLICT (norad_id) DO UPDATE SET
        {", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, satellites.{c})"
            for c in _SATELLITE_COLUMNS if c != "norad_id"
        )},
        last_updated = now()
""")


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

    with _tx() as conn:
        conn.execute(_UPSERT_SATELLITE_SQL, rows)
    logger.info("Upserted %d satellite rows.", len(rows))
    return len(rows)


_INSERT_TLE_HISTORY_SQL = text("""
    INSERT INTO tle_history (norad_id, line1, line2, epoch, source)
    VALUES (:norad_id, :line1, :line2, :epoch, :source)
    ON CONFLICT (norad_id, epoch) DO NOTHING
""")


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
    rows = [
        {
            "norad_id": r["norad_id"],
            "line1": r["line1"],
            "line2": r["line2"],
            "epoch": r["epoch"],
            "source": r.get("source", "spacetrack"),
        }
        for r in records
    ]
    if not rows:
        return 0
    with _tx() as conn:
        conn.execute(_INSERT_TLE_HISTORY_SQL, rows)
    logger.info("Inserted up to %d tle_history rows (duplicates skipped).", len(rows))
    return len(rows)


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
        {
            "norad_id": p["norad_id"],
            "timestamp": p["timestamp"],
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "altitude_km": p["altitude_km"],
            "velocity_km_s": p.get("velocity_km_s"),
            "azimuth_deg": p.get("azimuth_deg"),
            "elevation_deg": p.get("elevation_deg"),
            "range_km": p.get("range_km"),
        }
        for p in positions
    ]
    if not rows:
        return 0
    with _tx() as conn:
        conn.execute(_UPSERT_POSITION_SQL, rows)
    logger.info("Upserted %d orbital_positions rows.", len(rows))
    return len(rows)


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
    with _tx() as conn:
        result = conn.execute(
            text("DELETE FROM orbital_positions WHERE timestamp < :cutoff"),
            {"cutoff": datetime.now(timezone.utc) - timedelta(hours=hours)},
        )
        deleted = result.rowcount or 0
    logger.info("Pruned %d orbital_positions rows older than %dh.", deleted, hours)
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

_UPSERT_VISIBILITY_SQL = text("""
    INSERT INTO visibility_windows (
        norad_id, sensor_id, analysis_date, window_start, window_end,
        hour_bin, max_elevation, max_azimuth, min_range_km,
        orbit_regime, confidence_score
    ) VALUES (
        :norad_id, :sensor_id, :analysis_date, :window_start, :window_end,
        :hour_bin, :max_elevation, :max_azimuth, :min_range_km,
        :orbit_regime, :confidence_score
    )
    ON CONFLICT (norad_id, sensor_id, window_start) DO UPDATE SET
        window_end       = EXCLUDED.window_end,
        hour_bin          = EXCLUDED.hour_bin,
        max_elevation     = EXCLUDED.max_elevation,
        max_azimuth       = EXCLUDED.max_azimuth,
        min_range_km      = EXCLUDED.min_range_km,
        orbit_regime      = EXCLUDED.orbit_regime,
        confidence_score  = COALESCE(EXCLUDED.confidence_score, visibility_windows.confidence_score)
""")


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

    with _tx() as conn:
        conn.execute(_UPSERT_VISIBILITY_SQL, payload)
    logger.info(
        "Upserted %d visibility_windows rows for sensor=%s on %s.",
        len(payload), sensor_short_name, analysis_date,
    )
    return len(payload)


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
