"""
main.py

Main execution script.

Run:

python main.py

CHANGES FROM ORIGINAL:

1. PERFORMANCE: The original code called `.at(t)` / `.altaz()` once
   per satellite *per time step* (up to ~144 calls/satellite for a
   12-hour, 5-minute-step grid). This version builds one vectorized
   Skyfield Time array for the whole grid and propagates each
   satellite in a single call, which is dramatically faster across
   a full active-satellite catalog.

2. COVERAGE: The original code stopped at the satellite's first
   visible time point in the whole 12-hour window and ignored any
   later passes. This version evaluates every 1-hour bin in the
   window independently -- a satellite gets one row per hour bin in
   which it's visible, so a satellite with multiple passes across
   the day shows up multiple times instead of just once.

3. ROBUSTNESS: Per-satellite exceptions are now counted and
   reported instead of being silently swallowed.

4. OUTPUT: Azimuth, elevation, and range at the moment of detection
   are now included, since they were already being computed and
   discarded.
"""

import os
import sys
import json
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from tqdm import tqdm

from skyfield.api import wgs84

from config import *
from satellite_utils import (
    download_tle,
    download_tle_spacetrack,
    load_satellites,
    classify_orbit,
    in_sensor_field_of_regard
)

from catalog_diff import backup_current_catalog, compare_and_recommend
from sensor_select import prompt_for_date, prompt_for_timeframe, prompt_for_lookback_years, prompt_for_lookback_style, prompt_for_sensor
from log_utils import start_logging, log_run_settings, log_exception, finish_logging

ACCURACY_FILE = os.path.join(os.path.dirname(OUTPUT_FILE), "historical_accuracy_report.xlsx")


# =====================================================
# STEP 0
# ANALYSIS DATE + TIME WINDOW + SENSOR SELECTION
# =====================================================

print("=== Analysis Setup ===\n")

# Date first -- needed before sensor selection so the timezone
# conversion in prompt_for_timeframe uses the correct DST offset
# for the actual analysis date (e.g. CDT vs CST depends on whether
# the date falls in summer or winter).
_default_date  = START_TIME.split(" ")[0]
_analysis_date = prompt_for_date(_default_date)

# Sensor second -- timezone comes from the sensor profile.
_sensor = prompt_for_sensor()

SENSOR_LAT                    = _sensor["lat"]
SENSOR_LON                    = _sensor["lon"]
SENSOR_ELEV_M                 = _sensor["elev_m"]
MIN_ELEVATION_DEG             = _sensor["min_elevation_deg"]
MAX_ELEVATION_DEG             = _sensor["max_elevation_deg"]
APPLY_SENSOR_FIELD_OF_REGARD  = _sensor["apply_field_of_regard"]
SENSOR_BORESIGHT_AZIMUTH_DEG  = _sensor["boresight_azimuth_deg"]
SENSOR_AZIMUTH_HALF_WIDTH_DEG = _sensor["azimuth_half_width_deg"]

print(f"Sensor: {_sensor['name']}\n")

# Time window third -- prompt shows local time for the sensor's
# timezone and converts to Zulu automatically, using the analysis
# date to apply the correct DST offset.
_default_start = START_TIME.split(" ")[1][:5].replace(":", "")[:4]
_default_end   = END_TIME.split(" ")[1][:5].replace(":", "")[:4]

_local_start, _local_end, _utc_start, _utc_end, _tz_label = prompt_for_timeframe(
    analysis_date=_analysis_date,
    sensor_profile=_sensor,
    default_start=_default_start,
    default_end=_default_end,
)

# The analysis engine (Skyfield/SGP4) always works in UTC.
START_TIME        = f"{_analysis_date} {_utc_start[:2]}:{_utc_start[2:]}:00"
END_TIME          = f"{_analysis_date} {_utc_end[:2]}:{_utc_end[2:]}:00"
TARGET_EPOCH_DATE = _analysis_date

print(f"Analysis window:")
print(f"  Local : {_local_start} - {_local_end} {_tz_label}")
print(f"  Zulu  : {_utc_start}Z - {_utc_end}Z")
print()

_lookback_style, _lookback_value = prompt_for_lookback_style(
    default_years=HISTORICAL_LOOKBACK_YEARS
)
# _lookback_style is "single" or "smart_adaptive"
# _lookback_value is either an int (single) or dict {"max_years": N}
# For display/tagging purposes store a representative year value.
# smart_adaptive uses max_years; single uses the chosen value.
HISTORICAL_LOOKBACK_YEARS = (
    _lookback_value if _lookback_style == "single"
    else _lookback_value.get("max_years", 10)
)

# ── Build timestamped output filenames ────────────────────────
# Tag uses Zulu times (what the analysis actually ran against) and
# also records the local times + timezone in last_run.json so the
# user can always trace back what local time they entered.
_tag = f"{_analysis_date}_{_utc_start}-{_utc_end}Z"
_out_dir = os.path.dirname(OUTPUT_FILE)
_base    = os.path.join(_out_dir, f"visible_satellites_{_tag}.xlsx")
_acc     = os.path.join(_out_dir, f"historical_accuracy_report_{_tag}.xlsx")
_grp     = os.path.join(_out_dir, f"grouped_confidence_report_{_tag}.xlsx")
_tabs    = os.path.join(_out_dir, f"Final_Report_With_Confidence_Tabs_{_tag}.xlsx")

# Override the config.py path for this run.
OUTPUT_FILE   = _base
ACCURACY_FILE = _acc

# ── Start logging ──────────────────────────────────────────
# From this point on, everything printed to the terminal is also
# written to output/logs/run_<timestamp>_<tag>.log
_log_path = start_logging(_out_dir, run_tag=_tag)

log_run_settings({
    "Analysis date":         _analysis_date,
    "Local time window":     f"{_local_start} - {_local_end} {_tz_label}",
    "Zulu time window":      f"{_utc_start}Z - {_utc_end}Z",
    "Sensor":                _sensor["name"],
    "Lookback style":        _lookback_style,
        "Lookback years":        _lookback_value,
    "Data source":           DATA_SOURCE,
    "Time step (min)":       TIME_STEP_MINUTES,
    "Min elevation (deg)":   MIN_ELEVATION_DEG,
    "Field of regard":       APPLY_SENSOR_FIELD_OF_REGARD,
})

# Write a small state file so that historical_accuracy.py,
# grouped_report.py, and confidence_tabs_report.py always know
# which output file belongs to the most recent run, even when they
# are launched independently (not chained through main.py).
os.makedirs(_out_dir, exist_ok=True)
_last_run = {
    "date":                _analysis_date,
    "local_start_hhmm":    _local_start,
    "local_end_hhmm":      _local_end,
    "timezone_label":      _tz_label,
    "utc_start_hhmm":      _utc_start,
    "utc_end_hhmm":        _utc_end,
    "tag":                 _tag,
    "lookback_style":      _lookback_style,
        "lookback_value":      _lookback_value,
        "lookback_years":      HISTORICAL_LOOKBACK_YEARS,
    "visible_satellites":  _base,
    "accuracy_report":     _acc,
    "grouped_report":      _grp,
    "tabs_report":         _tabs,
}
with open(os.path.join(_out_dir, "last_run.json"), "w") as _f:
    json.dump(_last_run, _f, indent=2)
print(f"Output files will be tagged: {_tag}\n")


# =====================================================
# STEP 1
# DOWNLOAD SATELLITE CATALOG
# =====================================================

# Snapshot whatever catalog is currently on disk BEFORE this run's
# download overwrites it, so we can compare old vs. new afterward.
# Returns None on the very first run ever (nothing to back up yet).
_previous_catalog_snapshot = backup_current_catalog(TLE_FILE)

if DATA_SOURCE == "spacetrack":

    if not SPACETRACK_USERNAME or not SPACETRACK_PASSWORD:
        raise RuntimeError(
            "DATA_SOURCE is 'spacetrack' but SPACETRACK_USERNAME / "
            "SPACETRACK_PASSWORD are not set. Export them as "
            "environment variables before running."
        )

    target_date = datetime.strptime(TARGET_EPOCH_DATE, "%Y-%m-%d")

    download_tle_spacetrack(
        target_date,
        TLE_FILE,
        SPACETRACK_USERNAME,
        SPACETRACK_PASSWORD,
        window_days=SPACETRACK_WINDOW_DAYS,
        timeout_sec=REQUEST_TIMEOUT_SEC
    )

else:

    download_tle(
        TLE_URL,
        TLE_FILE,
        timeout_sec=REQUEST_TIMEOUT_SEC,
        max_cache_age_hours=CELESTRAK_CACHE_MAX_AGE_HOURS
    )

# Compare the freshly-downloaded (or cache-confirmed-unchanged)
# catalog against the previous snapshot, and get a recommendation
# on whether re-running historical_accuracy.py is worthwhile. This
# also naturally handles the "used cache, nothing changed" case --
# the snapshot and the current file end up identical, so the diff
# comes back empty and correctly recommends no re-run is needed.
_catalog_recommend_rerun = None
if _previous_catalog_snapshot:
    _catalog_recommend_rerun = compare_and_recommend(
        _previous_catalog_snapshot, TLE_FILE, ACCURACY_FILE
    )

# =====================================================
# STEP 2
# LOAD SATELLITES
# =====================================================

satellites, ts = load_satellites(
    TLE_FILE
)

# =====================================================
# STEP 3
# SENSOR LOCATION
# =====================================================

observer = wgs84.latlon(
    SENSOR_LAT,
    SENSOR_LON,
    elevation_m=SENSOR_ELEV_M
)

# =====================================================
# STEP 4
# BUILD TIME GRID
# =====================================================

start_dt = datetime.strptime(START_TIME, "%Y-%m-%d %H:%M:%S")
end_dt = datetime.strptime(END_TIME, "%Y-%m-%d %H:%M:%S")

time_grid = []
current = start_dt
while current <= end_dt:
    time_grid.append(current)
    current += timedelta(minutes=TIME_STEP_MINUTES)

print(f"Generated {len(time_grid)} time points.")

# Vectorized Skyfield time array covering the whole grid at once.
t_array = ts.utc(
    [dt.year for dt in time_grid],
    [dt.month for dt in time_grid],
    [dt.day for dt in time_grid],
    [dt.hour for dt in time_grid],
    [dt.minute for dt in time_grid],
    [dt.second for dt in time_grid],
)

# Assign each time-grid point to an hourly bin (0, 1, 2, ... per
# BIN_SIZE_HOURS-hour block) so "every hour in the window counts".
window_hours = (end_dt - start_dt).total_seconds() / 3600
num_bins = max(1, math.ceil(window_hours / BIN_SIZE_HOURS))

bin_seconds = BIN_SIZE_HOURS * 3600
bin_indices = np.array([
    min(int((dt - start_dt).total_seconds() // bin_seconds), num_bins - 1)
    for dt in time_grid
])

bin_labels = []
for b in range(num_bins):
    bin_start = start_dt + timedelta(hours=b * BIN_SIZE_HOURS)
    bin_end = bin_start + timedelta(hours=BIN_SIZE_HOURS)
    bin_labels.append(
        f"{bin_start.strftime('%H%M')}-{bin_end.strftime('%H%M')}Z"
    )

# =====================================================
# STEP 5
# VISIBILITY ANALYSIS (one row per satellite per visible hour bin)
# =====================================================

results = []
error_count = 0

print(f"Processing {len(satellites):,} satellites...", flush=True)
for sat in tqdm(satellites, desc="Satellites", unit="sat",
                file=sys.__stderr__, dynamic_ncols=True):

    try:
        orbit_type = classify_orbit(sat)
        satnum = sat.model.satnum

        difference = sat - observer
        topocentric = difference.at(t_array)

        altitude, azimuth, distance = topocentric.altaz()

        if APPLY_SENSOR_FIELD_OF_REGARD:
            visible_mask = in_sensor_field_of_regard(
                altitude.degrees,
                azimuth.degrees,
                MIN_ELEVATION_DEG,
                SENSOR_BORESIGHT_AZIMUTH_DEG,
                SENSOR_AZIMUTH_HALF_WIDTH_DEG,
                max_elevation_deg=MAX_ELEVATION_DEG
            )
        else:
            visible_mask = (altitude.degrees >= MIN_ELEVATION_DEG) & (altitude.degrees <= MAX_ELEVATION_DEG)

        if not visible_mask.any():
            continue

        for b in range(num_bins):

            in_bin = (bin_indices == b) & visible_mask
            idxs = np.where(in_bin)[0]

            if len(idxs) == 0:
                continue

            first_idx = idxs[0]
            dt = time_grid[first_idx]

            results.append({
                "Date": dt.strftime("%d-%b-%Y"),
                "Hour Window": bin_labels[b],
                "Design Point": DESIGN_POINT_ID,
                "Time (Zulu)": dt.strftime("%H%M"),
                "Target Name": sat.name,
                "Target Orbit": orbit_type,
                "Target NORAD": satnum,
                "Elevation (deg)": round(float(altitude.degrees[first_idx]), 2),
                "Azimuth (deg)": round(float(azimuth.degrees[first_idx]), 2),
                "Range (km)": round(float(distance.km[first_idx]), 1),
            })

    except Exception:
        error_count += 1
        continue

print(f"Visibility analysis complete. {error_count} satellites raised errors and were skipped.")

# =====================================================
# STEP 6
# EXPORT
# =====================================================

# Save visibility results to Excel
_df = pd.DataFrame(results)
if len(_df) == 0:
    print("No visible satellites found.")
else:
    _df = _df.sort_values(by=["Hour Window", "Target Orbit", "Target Name"])
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    _df.to_excel(OUTPUT_FILE, index=False)
    print(f"Saved {len(_df)} rows.")

print("Processing complete.")

# =====================================================
# STEP 7
# OPTIONALLY CHAIN INTO THE HISTORICAL ACCURACY EVALUATION
# =====================================================

print()
if _catalog_recommend_rerun is True:
    print("(Based on the catalog comparison above, re-running is recommended.)")
elif _catalog_recommend_rerun is False:
    print("(Based on the catalog comparison above, a re-run doesn't appear necessary right now.)")

# ── Auto-decide online vs offline ────────────────────────────────────
# Check locally (zero network calls) whether any Space-Track data is
# actually needed. The user just says y/n -- the tool figures out
# whether to go online or use the local cache automatically.

def _decide_online_or_offline():
    from datetime import date as _date
    import os as _os
    try:
        import api_request_log
        import tle_history_cache as _thc
        import tle_bulk_seeder as _tbs

        today   = _date.today()
        lookback = (
            _lookback_value if _lookback_style == "single"
            else _lookback_value.get("max_years", HISTORICAL_LOOKBACK_YEARS)
        )
        end_d   = today
        start_d = _date(end_d.year - lookback, end_d.month, end_d.day)

        # How many satellites need a gp_history fetch?
        norad_ids = []
        try:
            import pandas as _pd
            df = _pd.read_excel(OUTPUT_FILE, usecols=["Target NORAD"])
            norad_ids = df["Target NORAD"].dropna().astype(int).unique().tolist()
        except Exception:
            pass
        _, needs_fetch = _thc.split_cached_vs_needed(
            TLE_HISTORY_CACHE_DB, norad_ids, start_d, end_d
        ) if norad_ids else ([], [])
        needs_gp = len(needs_fetch)

        # Has SATCAT been fetched today?
        counts = api_request_log.get_recent_counts(API_REQUEST_LOG_DB)
        satcat_done  = counts["satcat_fetches_today"] > 0

        # Does today's daily GP snapshot already exist?
        snap_done = _os.path.exists(_tbs.daily_zip_path(TLE_DATA_DIR, today))

        snap_label_yes = "NO  — today's snapshot already exists"
        snap_label_no  = "YES — will capture today's catalog"
        reasons = [
            f"  gp_history  : {'NO  — all ' + str(len(norad_ids)) + ' satellites already cached' if needs_gp == 0 else 'YES — ' + str(needs_gp) + ' satellite(s) need fetching'}",
            f"  SATCAT      : {'NO  — already fetched today' if satcat_done else 'YES — will fetch (1/day)'}",
            f"  GP snapshot : {snap_label_yes if snap_done else snap_label_no}",
        ]

        needs_online = needs_gp > 0 or not satcat_done or not snap_done
        return ("online" if needs_online else "offline"), reasons

    except Exception as e:
        return "online", [f"  (Could not auto-check: {e} — defaulting to online)"]


_auto_mode, _auto_reasons = _decide_online_or_offline()

print(
    f"\nSpace-Track data needed for this run:\n"
    + "\n".join(_auto_reasons)
)
if _auto_mode == "offline":
    print(
        "\n→ Nothing to fetch. Will score entirely from local cache "
        "(no Space-Track connection)."
    )
else:
    print(
        "\n→ Some data needs fetching. Will connect to Space-Track "
        "only for what's missing."
    )

print()
response = input(
    "Run historical accuracy evaluation? (y/n): "
).strip().lower()

if response in ("y", "yes"):
    _offline = (_auto_mode == "offline")
    if _offline:
        print(
            "\nStep 7: Running historical confidence evaluation "
            "(offline -- all data already cached)...\n"
        )
    else:
        print(
            "\nStep 7: Running historical confidence evaluation "
            "(online -- fetching missing data from Space-Track)...\n"
        )
    try:
        import historical_accuracy
        historical_accuracy.main(
            output_file=OUTPUT_FILE,
            accuracy_file=ACCURACY_FILE,
            lookback_years=_lookback_value,
            lookback_style=_lookback_style,
            offline_mode=_offline,
        )
    except Exception:
        log_exception("historical_accuracy.main()")
        print(
            "\n  The confidence evaluation failed -- see the log file for\n"
            "  the full error details:\n"
            f"    {_log_path}\n"
        )
        if not _offline:
            print(
                "  If Space-Track was unavailable, re-run and the tool\n"
                "  will retry only what failed.\n"
            )
else:
    print(
        "Skipping historical accuracy evaluation. "
        "Run 'python historical_accuracy.py' later if you want it."
    )

# =====================================================
# STEP 8
# BUILD THE GROUPED + PER-CONFIDENCE-TAB REPORTS
# =====================================================

# Both report scripts need historical_accuracy_report.xlsx to exist
# (whether it was just generated above, or already existed from an
# earlier run) -- they can't assign confidence categories without
# it. If it's still missing (user said no above and no earlier run
# exists either), skip these with a clear explanation instead of
# letting them crash.

print()

if os.path.exists(ACCURACY_FILE):

    print("Step 8: Generating Excel reports...")

    try:
        import reports
        reports.generate_grouped_report(
            output_file=OUTPUT_FILE,
            accuracy_file=ACCURACY_FILE,
            report_file=_grp,
        )
    except Exception:
        log_exception("grouped_report.main()")
        print("  grouped_confidence_report failed -- see log for details.")

    print()

    try:
        reports.generate_tabs_report(
            output_file=OUTPUT_FILE,
            accuracy_file=ACCURACY_FILE,
            report_file=_tabs,
        )
    except Exception:
        log_exception("confidence_tabs_report.main()")
        print("  Final_Report_With_Confidence_Tabs failed -- see log for details.")

    print()
    print("─" * 60)
    print(f"All done! Output files tagged: {_tag}")
    print(f"  output/")
    for label, path in [
        ("Visibility results",   OUTPUT_FILE),
        ("Confidence scores",    ACCURACY_FILE),
        ("Grouped report",       _grp),
        ("Confidence tabs",      _tabs),
    ]:
        status = "OK" if os.path.exists(path) else "NOT CREATED"
        print(f"    [{status:<11}] {os.path.basename(path):<48} {label}")
    print("─" * 60)

else:
    print(
        "Step 8: Skipping Excel report generation.\n"
        f"  Reason: {os.path.basename(_acc)} does not exist yet.\n"
        "\n"
        "  The reports need confidence scores to assign colors and tabs.\n"
        "  Re-run main.py and answer \'y\' when asked to run the\n"
        "  historical confidence evaluation, OR run\n"
        "  \'python historical_accuracy.py\' then re-run main.py.\n"
        "\n"
        f"  Your visibility results ARE saved:\n"
        f"    {os.path.basename(OUTPUT_FILE)}"
    )

# ── Finish logging ─────────────────────────────────────────
finish_logging({
    "Visibility results":  OUTPUT_FILE,
    "Confidence scores":   ACCURACY_FILE,
    "Grouped report":      _grp,
    "Confidence tabs":     _tabs,
})
