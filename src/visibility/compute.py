"""
Headless visibility windows - which satellites a sensor can see, when.

WHY THIS EXISTS RATHER THAN A --headless FLAG ON src/tracking/main.py
=====================================================================
`src/tracking/main.py` is a 586-line top-level script with five interactive
prompts and no `def main()`. It serves an analyst at a keyboard and produces
Excel. Bolting an unattended mode onto it would mean refactoring working
code that someone depends on, and would leave one file serving two very
different callers.

This module is the pipeline's own path: parameters in, database rows out,
no prompts, no Excel. The interactive tool is untouched.

Results should match it. Both use skyfield for topocentric alt/az and the
same `in_sensor_field_of_regard` from satellite_utils, with the same
5-minute sampling and 1-hour binning, and the same "first visible sample in
the bin" convention (main.py's `first_idx = idxs[0]`).

WHY SKYFIELD HERE, WHEN THE PROPAGATOR DELIBERATELY AVOIDS IT
=============================================================
`src/propagation/propagator.py` uses sgp4 alone to keep the 2-hourly TLE
workflow's install slim and avoid a runtime timescale download. This is a
once-daily job, and matching the interactive tool's numbers exactly matters
more than a lean install - so it uses skyfield, with `builtin=True` so no
timescale file is fetched at runtime.

SENSORS COME FROM THE DATABASE
==============================
`sensor_select.SENSOR_PROFILES` and the `sensors` table had drifted: the
table has GEODSS_SOC and MILLSTONE that the tool does not, and the tool has
PAVE PAWS that the table does not. The table carries every field the
computation needs, and `visibility_windows.sensor_id` is a foreign key to
it - so it is the source of truth here. Adding a sensor to the database is
enough to make it available to this job.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

#: Matches src/tracking/config.py so results line up with the Excel reports.
DEFAULT_STEP_MINUTES = 5
DEFAULT_BIN_HOURS = 1

#: TLEs older than this are not propagated - same reasoning as the
#: propagator: SGP4 error grows quickly and a stale set is confidently wrong.
DEFAULT_MAX_AGE_DAYS = 14

#: Retention, against a 500 MB tier.
#:
#: Measured 2026-09-01 on the live 18,044-object catalogue: 221,788
#: windows per day across the three sensors, at a measured 215 bytes per
#: row - so 47.7 MB every day. Seven days would be 334 MB, and
#: tle_history needs 113 MB of the same budget once pruned. Three days is
#: 143 MB and leaves the database around 291 MB with room to grow.
#:
#: This started at 7 from an estimated 180 bytes/row. The estimate was
#: 19% low and the tier was already 97 MB over, so the number moved. The
#: job reports the table's real size after every write; widen this the
#: moment the budget allows, because coverage statistics want more days
#: than three.
DEFAULT_RETENTION_DAYS = 3

#: Individual element sets do fail to parse; a run where most of them do is
#: a bug in this module, not bad data, and must not exit 0 with an empty
#: table. Measured baseline on the live catalogue: see the sprint notes.
MAX_ERROR_FRACTION = 0.05


@dataclass
class Sensor:
    """One row of the `sensors` table."""
    id: int
    short_name: str
    name: str
    latitude: float
    longitude: float
    elevation_m: float
    min_elevation_deg: float
    max_elevation_deg: float
    boresight_azimuth_deg: Optional[float]
    azimuth_half_width_deg: Optional[float]
    apply_field_of_regard: bool


# ══════════════════════════════════════════════════════════════════════════════
#  Sensors
# ══════════════════════════════════════════════════════════════════════════════

_SENSOR_SQL = """
    SELECT id, short_name, name, latitude, longitude,
           COALESCE(elevation_m, 0), COALESCE(min_elevation_deg, 0),
           COALESCE(max_elevation_deg, 90), boresight_azimuth_deg,
           azimuth_half_width_deg, COALESCE(apply_field_of_regard, TRUE)
    FROM sensors
"""


def _row_to_sensor(r) -> Sensor:
    return Sensor(id=r[0], short_name=r[1], name=r[2], latitude=float(r[3]),
                  longitude=float(r[4]), elevation_m=float(r[5]),
                  min_elevation_deg=float(r[6]), max_elevation_deg=float(r[7]),
                  boresight_azimuth_deg=(None if r[8] is None else float(r[8])),
                  azimuth_half_width_deg=(None if r[9] is None else float(r[9])),
                  apply_field_of_regard=bool(r[10]))


def load_sensors() -> List[Sensor]:
    """Every sensor in the database."""
    from sqlalchemy import text
    from src.db.writer import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(text(_SENSOR_SQL + " ORDER BY short_name")).fetchall()
    return [_row_to_sensor(r) for r in rows]


def load_sensor(short_name: str) -> Sensor:
    """One sensor by short name, e.g. 'FPS85'."""
    from sqlalchemy import text
    from src.db.writer import get_engine
    with get_engine().connect() as conn:
        row = conn.execute(text(_SENSOR_SQL + " WHERE short_name = :s"),
                           {"s": short_name}).fetchone()
    if row is None:
        available = ", ".join(s.short_name for s in load_sensors())
        raise ValueError(
            f"No sensor with short_name {short_name!r}. Available: {available}"
        )
    return _row_to_sensor(row)


# ══════════════════════════════════════════════════════════════════════════════
#  Visibility
# ══════════════════════════════════════════════════════════════════════════════

def _bin_label(start: datetime, bin_hours: int) -> str:
    """'0600-0700Z' - the format insert_visibility_windows parses."""
    end = start + timedelta(hours=bin_hours)
    return f"{start.strftime('%H%M')}-{end.strftime('%H%M')}Z"


def _check_tle_lines(line1: str, line2: str) -> None:
    """
    Reject structurally wrong element sets before sgp4 sees them.

    sgp4 is lenient: given a line of prose it returns a satellite whose
    epoch lands in 1949 rather than raising. That satellite then trips the
    staleness check and is counted as "skipped", so a whole catalogue of
    garbage looks like a whole catalogue of old-but-valid TLEs and the run
    reports success with nothing written. Fail on the shape instead.
    """
    for n, line in ((1, line1), (2, line2)):
        if not isinstance(line, str) or len(line.rstrip()) < 68:
            raise ValueError(f"TLE line {n} is too short to be an element set")
        if line[0] != str(n) or line[1] != " ":
            raise ValueError(f"TLE line {n} does not start with '{n} '")


def _classify_regime(mean_motion_rev_per_day: float) -> str:
    """LEO/MEO/GEO/HEO from mean motion, matching the fetcher's convention."""
    if mean_motion_rev_per_day <= 0:
        return "UNKNOWN"
    period_min = 1440.0 / mean_motion_rev_per_day
    if period_min < 128:
        return "LEO"
    if period_min < 1400:
        return "MEO"
    if period_min < 1500:
        return "GEO"
    return "HEO"


def compute_visibility(
    sensor: Sensor,
    tles: Sequence,
    analysis_date: date,
    start_hhmm: str = "0000",
    end_hhmm: str = "2359",
    step_minutes: int = DEFAULT_STEP_MINUTES,
    bin_hours: int = DEFAULT_BIN_HOURS,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> List[Dict[str, Any]]:
    """
    One row per satellite per hour bin in which it is visible.

    `tles` are propagator.TLE objects. Rows come back in the shape
    `db.writer.insert_visibility_windows()` consumes, so they can be passed
    straight through.
    """
    import numpy as np
    from skyfield.api import EarthSatellite, load, wgs84

    from src.tracking.satellite_utils import in_sensor_field_of_regard

    ts = load.timescale(builtin=True)          # builtin: no runtime download
    observer = wgs84.latlon(sensor.latitude, sensor.longitude,
                            elevation_m=sensor.elevation_m)

    start_dt = datetime.combine(
        analysis_date,
        time(int(start_hhmm[:2]), int(start_hhmm[2:])), tzinfo=timezone.utc)
    end_dt = datetime.combine(
        analysis_date,
        time(int(end_hhmm[:2]), int(end_hhmm[2:])), tzinfo=timezone.utc)
    if end_dt <= start_dt:
        raise ValueError(f"end {end_hhmm} is not after start {start_hhmm}")

    # Time grid, and which bin each sample belongs to
    total_minutes = int((end_dt - start_dt).total_seconds() // 60)
    grid = [start_dt + timedelta(minutes=m)
            for m in range(0, total_minutes + 1, step_minutes)]
    t_array = ts.utc([d.year for d in grid], [d.month for d in grid],
                     [d.day for d in grid], [d.hour for d in grid],
                     [d.minute for d in grid], [d.second for d in grid])

    num_bins = max(1, math.ceil(total_minutes / (bin_hours * 60)))
    bin_of_sample = np.array(
        [min(int((d - start_dt).total_seconds() // (bin_hours * 3600)),
             num_bins - 1) for d in grid])
    bin_labels = [_bin_label(start_dt + timedelta(hours=b * bin_hours), bin_hours)
                  for b in range(num_bins)]

    if sensor.apply_field_of_regard and sensor.boresight_azimuth_deg is None:
        # Quiet drift is how this project has been bitten five times: a row
        # says "apply the field of regard" and the field it needs is NULL,
        # so the mask silently widens to the whole sky and the run looks fine
        # while reporting satellites this sensor cannot actually see.
        log.warning(
            "Sensor %s has apply_field_of_regard=TRUE but no "
            "boresight_azimuth_deg; falling back to a plain %.1f-%.1f deg "
            "elevation mask. Fix the sensors row or set the flag FALSE.",
            sensor.short_name, sensor.min_elevation_deg,
            sensor.max_elevation_deg)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    results: List[Dict[str, Any]] = []
    skipped = errors = 0
    first_error: Optional[BaseException] = None

    for tle in tles:
        try:
            _check_tle_lines(tle.line1, tle.line2)
            sat = EarthSatellite(tle.line1, tle.line2, tle.name, ts)
            epoch = sat.epoch.utc_datetime()
            if epoch < cutoff:
                skipped += 1
                continue

            topocentric = (sat - observer).at(t_array)
            alt, az, dist = topocentric.altaz()
            alt_deg, az_deg, range_km = alt.degrees, az.degrees, dist.km

            if sensor.apply_field_of_regard and sensor.boresight_azimuth_deg is not None:
                visible = in_sensor_field_of_regard(
                    alt_deg, az_deg,
                    min_elevation_deg=sensor.min_elevation_deg,
                    boresight_azimuth_deg=sensor.boresight_azimuth_deg,
                    azimuth_half_width_deg=sensor.azimuth_half_width_deg or 180.0,
                    max_elevation_deg=sensor.max_elevation_deg,
                )
            else:
                visible = ((alt_deg >= sensor.min_elevation_deg)
                           & (alt_deg <= sensor.max_elevation_deg))

            if not visible.any():
                continue

            regime = _classify_regime(sat.model.no_kozai * 1440.0 / (2.0 * math.pi))

            for b in range(num_bins):
                idxs = np.flatnonzero(visible & (bin_of_sample == b))
                if idxs.size == 0:
                    continue
                # First visible sample in the bin - matches main.py's
                # `first_idx = idxs[0]`, so the two agree. See the note in
                # insert_visibility_windows about these not being true maxima.
                i = idxs[0]
                results.append({
                    "Hour Window": bin_labels[b],
                    "Target Name": tle.name,
                    "Target Orbit": regime,
                    "Target NORAD": tle.norad_id,
                    "Elevation (deg)": round(float(alt_deg[i]), 2),
                    "Azimuth (deg)": round(float(az_deg[i]), 2),
                    "Range (km)": round(float(range_km[i]), 1),
                })
        except Exception as exc:
            # A malformed element set in a 18,000-row catalogue is expected
            # and should not stop the run. A bug in this function is not,
            # and a bare `continue` would turn one into the other: every
            # satellite would "error", the job would exit 0, and the table
            # would stay empty. So keep the first exception and re-raise
            # below if the failure rate says this is not bad data.
            errors += 1
            if first_error is None:
                first_error = exc
                log.warning("First per-satellite failure (%s): %s: %s",
                            tle.norad_id, type(exc).__name__, exc)
            continue

    attempted = len(tles) - skipped
    if tles and attempted == 0:
        raise RuntimeError(
            f"All {len(tles)} element sets are older than {max_age_days} "
            f"days. Nothing was computed. Check that the TLE fetcher is "
            f"still running before treating this as an empty sky."
        )
    if attempted and errors / attempted > MAX_ERROR_FRACTION:
        raise RuntimeError(
            f"{errors} of {attempted} satellites failed "
            f"({errors / attempted:.0%}, threshold "
            f"{MAX_ERROR_FRACTION:.0%}). This is a defect in the "
            f"computation, not bad element sets."
        ) from first_error

    log.info("Visibility: %d windows from %d satellites "
             "(%d stale >%dd, %d errored).",
             len(results), len(tles), skipped, max_age_days, errors)
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute visibility windows and write them to the database")
    parser.add_argument("--sensor", help="Sensor short_name, e.g. FPS85")
    parser.add_argument("--all-sensors", action="store_true",
                        help="Every sensor in the database, one TLE load "
                             "shared between them")
    parser.add_argument("--list-sensors", action="store_true",
                        help="Show sensors in the database and exit")
    parser.add_argument("--sizes", action="store_true",
                        help="Report table sizes against the tier budget "
                             "and exit")
    parser.add_argument("--date", help="Analysis date YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--start", default="0000", help="Window start HHMM UTC")
    parser.add_argument("--end", default="2359", help="Window end HHMM UTC")
    parser.add_argument("--step-minutes", type=int, default=DEFAULT_STEP_MINUTES)
    parser.add_argument("--bin-hours", type=int, default=DEFAULT_BIN_HOURS)
    parser.add_argument("--limit", type=int,
                        help="Only this many satellites (smoke test)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and report, but do not write")
    parser.add_argument("--prune", action="store_true",
                        help="After writing, delete windows older than "
                             "--prune-days")
    parser.add_argument("--prune-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"Retention window (default "
                             f"{DEFAULT_RETENTION_DAYS})")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        from src.env import load_env
        load_env()
    except ImportError:
        pass

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s")

    if args.list_sensors:
        print(f"\n  {'short_name':<14}{'name':<28}{'lat':>9}{'lon':>10}  FoR")
        for s in load_sensors():
            forr = (f"{s.boresight_azimuth_deg:.0f}+-{s.azimuth_half_width_deg:.0f}"
                    if s.apply_field_of_regard and s.boresight_azimuth_deg is not None
                    else "full sky")
            print(f"  {s.short_name:<14}{s.name[:27]:<28}"
                  f"{s.latitude:>9.2f}{s.longitude:>10.2f}  {forr}")
        return 0

    if args.sizes:
        report_budget(retention_days=args.prune_days)
        return 0

    if not args.sensor and not args.all_sensors:
        parser.error("--sensor or --all-sensors is required "
                     "(or use --list-sensors)")

    analysis_date = (datetime.strptime(args.date, "%Y-%m-%d").date()
                     if args.date else datetime.now(timezone.utc).date())

    sensors = load_sensors() if args.all_sensors else [load_sensor(args.sensor)]
    for s in sensors:
        log.info("Sensor %s (%s) at %.3f, %.3f",
                 s.short_name, s.name, s.latitude, s.longitude)

    # One load for every sensor. The catalogue is the same 18,000 rows
    # whichever site is looking at it, and fetching it once per sensor was
    # three round trips for identical data.
    from src.propagation.propagator import load_tles
    tles = load_tles(limit=args.limit)
    log.info("Loaded %d TLEs.", len(tles))
    if not tles:
        log.warning("Nothing to analyse - has the TLE fetcher run?")
        return 1

    total_written = 0
    for sensor in sensors:
        rows = compute_visibility(
            sensor, tles, analysis_date,
            start_hhmm=args.start, end_hhmm=args.end,
            step_minutes=args.step_minutes, bin_hours=args.bin_hours)

        if not rows:
            log.warning("%s: no satellites visible in this window.",
                        sensor.short_name)
            continue

        if args.dry_run:
            log.info("%s: dry run - not writing. First 5 of %d:",
                     sensor.short_name, len(rows))
            for r in rows[:5]:
                log.info("  %-10s %-24s el=%6.2f az=%7.2f range=%8.1f km",
                         r["Hour Window"], r["Target Name"][:24],
                         r["Elevation (deg)"], r["Azimuth (deg)"],
                         r["Range (km)"])
            continue

        from src.db.writer import insert_visibility_windows
        total_written += insert_visibility_windows(
            rows, sensor_short_name=sensor.short_name,
            analysis_date=analysis_date, bin_size_hours=args.bin_hours)

    if args.dry_run:
        return 0

    log.info("Wrote %d visibility windows across %d sensor(s).",
             total_written, len(sensors))

    if args.prune:
        from src.db.writer import prune_old_visibility_windows
        prune_old_visibility_windows(days=args.prune_days)

    report_budget(retention_days=args.prune_days)
    return 0


def report_budget(retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    """
    What the stored rows actually cost, and what the retention window
    projects to.

    Bytes per row is computed from the rows that are really in the table,
    not from the rows this run happened to write. An earlier version
    divided by `written * retention_days`, which silently assumed the
    retention window was already full - on day one that under-reported
    215 bytes/row as 31, an error of 7x in the reassuring direction.
    """
    from sqlalchemy import text

    from src.db.writer import database_sizes, get_engine, table_size_bytes

    size = table_size_bytes("visibility_windows")
    if size is None:
        return

    with get_engine().connect() as conn:
        rows, days = conn.execute(text(
            "SELECT count(*), count(DISTINCT analysis_date) "
            "FROM visibility_windows")).fetchone()

    if rows:
        per_row = size / rows
        per_day = rows / max(days, 1)
        log.info("visibility_windows: %.1f MB, %d rows over %d day(s) "
                 "= %.0f bytes/row, %.0f rows/day.",
                 size / 1e6, rows, days, per_row, per_day)
        log.info("Projected at %d-day retention: %.0f MB.",
                 retention_days, per_row * per_day * retention_days / 1e6)

    tables = database_sizes()
    if tables:
        total = sum(b for _, b in tables)
        log.info("Database total: %.0f MB across %d tables.",
                 total / 1e6, len(tables))
        for name, b in tables[:5]:
            log.info("    %-24s %7.1f MB", name, b / 1e6)


if __name__ == "__main__":
    sys.exit(main())
