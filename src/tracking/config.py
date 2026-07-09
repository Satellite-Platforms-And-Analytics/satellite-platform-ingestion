"""
config.py

Project configuration settings.

Modify this file to change:
- Sensor location
- Analysis window
- Visibility constraints
"""

import os
from dotenv import load_dotenv

# Loads variables from a local .env file (if present) into the
# environment. This means SPACETRACK_USERNAME / SPACETRACK_PASSWORD
# can live in a .env file instead of needing to be re-exported in
# every terminal session -- important in VS Code, where the "Run
# Python File" button opens a new terminal each time that won't
# have variables set in a previous terminal.
load_dotenv()

# =====================================================
# SENSOR INFORMATION (DEFAULTS)
# =====================================================

# These are DEFAULTS used by standalone scripts that don't prompt
# interactively (e.g. compare_n2yo.py, fetch_data.py). main.py
# instead prompts for a sensor at runtime (see sensor_select.py,
# which has the full registry of built-in sensor profiles) and
# overrides these for that run -- so changing these values only
# affects scripts that don't go through that prompt.
#
# Default: AN/FPS-85 (Eglin AFB, FL).
SENSOR_LAT = 30.57
SENSOR_LON = -86.21
SENSOR_ELEV_M = 40.0

# =====================================================
# ANALYSIS WINDOW
# =====================================================

START_TIME = "2026-08-05 06:00:00"
END_TIME = "2026-08-05 18:00:00"

# Time resolution (minutes). Should evenly divide 60 so hourly
# bins line up cleanly with the time grid.
TIME_STEP_MINUTES = 5

# Minimum (and maximum) elevation above horizon. Defaults match
# AN/FPS-85's documented ~3 deg elevation floor and effectively-90
# deg ceiling; main.py overrides both per the selected sensor.
MIN_ELEVATION_DEG = 3
MAX_ELEVATION_DEG = 90

# =====================================================
# SENSOR FIELD OF REGARD (DEFAULTS)
# =====================================================

# A fixed phased-array antenna faces a single direction -- not a
# fully steerable sensor -- so "visible" should mean "within the
# radar's actual beam coverage," not generic full-sky
# horizon-to-horizon visibility. These defaults match AN/FPS-85;
# main.py overrides them per the sensor chosen at the prompt.
#
# Set APPLY_SENSOR_FIELD_OF_REGARD = False to ignore this and fall
# back to generic full-sky horizon visibility instead (e.g. to
# compare against a hypothetical fully-steerable sensor at the
# same location).
APPLY_SENSOR_FIELD_OF_REGARD = True

SENSOR_BORESIGHT_AZIMUTH_DEG = 180.0   # due south
SENSOR_AZIMUTH_HALF_WIDTH_DEG = 60.0   # total az coverage = 120 deg

# =====================================================
# HOURLY BINNING
# =====================================================

# The analysis window is broken into consecutive 1-hour bins.
# A satellite is reported once per bin in which it is visible
# (i.e. every hour in the window counts independently), rather
# than only reporting the satellite's single first appearance
# across the whole window.
BIN_SIZE_HOURS = 1

# =====================================================
# MISC / OUTPUT FIELDS
# =====================================================

# Placeholder sensor tasking / design-point identifier. This was
# previously a hardcoded "1" baked into main.py with no
# explanation; it now lives here so it's an explicit, documented
# configuration value instead of a magic number.
DESIGN_POINT_ID = 1

# Network request timeout (seconds) for downloading the TLE catalog.
REQUEST_TIMEOUT_SEC = 30

# CelesTrak only refreshes GP data every 2 hours and throttles
# requests for the same group made sooner than that. Re-downloading
# more often than this just gets you a throttle notice back, so the
# script reuses a local cached file younger than this many hours.
CELESTRAK_CACHE_MAX_AGE_HOURS = 2

# =====================================================
# DATA SOURCE
# =====================================================

# "celestrak"  -> always pulls the current/live catalog. Good for
#                 "right now" analysis, but elements degrade in
#                 accuracy quickly and can't reproduce a past date.
# "spacetrack" -> pulls historical TLEs valid nearest to
#                 TARGET_EPOCH_DATE below. Requires a free
#                 Space-Track.org account. Use this to reproduce
#                 an analysis for a specific past date (e.g. once
#                 the 5 Aug 2026 window below has actually passed).
DATA_SOURCE = "celestrak"

# Space-Track credentials. Set these as environment variables
# rather than hardcoding them in this file:
#   export SPACETRACK_USERNAME="you@example.com"
#   export SPACETRACK_PASSWORD="your-password"
SPACETRACK_USERNAME = os.environ.get("SPACETRACK_USERNAME")
SPACETRACK_PASSWORD = os.environ.get("SPACETRACK_PASSWORD")

# Date to retrieve historical TLEs nearest to, when DATA_SOURCE is
# "spacetrack". Normally this should match the date in START_TIME.
TARGET_EPOCH_DATE = "2026-08-05"

# How many days before/after TARGET_EPOCH_DATE to search when
# looking for each satellite's nearest available TLE epoch. Wider
# windows catch more satellites (not all are updated daily) at the
# cost of a larger query and slightly staler elements for some.
SPACETRACK_WINDOW_DAYS = 3

# =====================================================
# HISTORICAL ACCURACY EVALUATION
# =====================================================

# How many years back to pull orbital history for each satellite
# when scoring prediction confidence (historical_accuracy.py).
#
# Reduced from 10 to 5: at large satellite counts (e.g. tens of
# thousands), a 10-year lookback across the whole catalog pulls a
# very large volume of data, since densely-tracked objects (LEO
# megaconstellations especially) generate multiple TLEs per day.
# Meaningful instability -- drag decay, maneuvers, orbit changes --
# is almost always visible well within a 5-year window; going back
# a full decade mostly adds query volume without changing the
# confidence score much for the vast majority of satellites. Raise
# this back toward 10 if you want deeper history for a smaller,
# more targeted satellite list.
HISTORICAL_LOOKBACK_YEARS = 5

# Cap on how many unique satellites from output/visible_satellites.xlsx
# to evaluate. Each satellite costs one Space-Track query, so this
# keeps a large results file from generating an excessive number of
# requests. Set to None to evaluate every unique satellite in the
# results with no cap (with ~30 Space-Track requests/minute, this
# can take a while for a large results file -- e.g. roughly 15-20
# minutes per 500 satellites).
HISTORICAL_MAX_SATELLITES = None

# How many satellites to bundle into a single Space-Track query.
# Increased from 50 to 100: each batch is one network request, so
# doubling the batch size roughly halves the total number of
# requests and cuts wall-clock time proportionally, while staying
# well within Space-Track's payload limits.
HISTORICAL_BATCH_SIZE = 100

# Batched historical queries pull much more data than this
# project's other API calls (a single CelesTrak download, a
# Space-Track login, etc.), so they get their own, longer timeout
# rather than sharing REQUEST_TIMEOUT_SEC. If batches are still
# timing out at this value, get_historical_orbital_elements_batch
# automatically retries with smaller sub-batches rather than
# dropping the whole batch's satellites from the results.
HISTORICAL_QUERY_TIMEOUT_SEC = 120

# How many batch requests to run concurrently. Space-Track's rate
# limit is about request-initiation frequency (30/min, 300/hour),
# not how many requests can be in flight at once -- so running
# several batches concurrently, all sharing one rate limiter, uses
# the same budget far more efficiently than sending batches one at
# a time and waiting for each full response before starting the
# next. Increased from 5 to 8 for better parallelism.
HISTORICAL_CONCURRENT_REQUESTS = 8

# =====================================================
# CATALOG CHANGE DETECTION
# =====================================================

# Thresholds for flagging a satellite's orbit as having changed
# "significantly" between two TLE catalog snapshots (i.e. an actual
# maneuver or real orbital change, vs. ordinary day-to-day drag
# decay / measurement noise). Used to recommend whether re-running
# historical_accuracy.py is worthwhile after a fresh download.
CATALOG_ALTITUDE_CHANGE_THRESHOLD_KM = 5.0
CATALOG_INCLINATION_CHANGE_THRESHOLD_DEG = 0.05

# =====================================================
# FILES
# =====================================================

# Anchor file paths to this config.py file's own location rather
# than the terminal's current working directory. Without this,
# "data/active.tle" would resolve relative to wherever the
# terminal happens to be cd'd into when you run the script (which
# may not be the project folder at all -- e.g. if you launch the
# script by absolute path from an unrelated directory).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TLE_FILE = os.path.join(BASE_DIR, "data", "active.tle")

# Cached copy of Jonathan McDowell's GCAT (General Catalog of Artificial
# Space Objects), used as a supplementary fallback for launch metadata
# alongside Space-Track's SATCAT (see historical_accuracy.py). GCAT is
# only updated roughly monthly, so the cache is reused across runs --
# see GCAT_CACHE_MAX_AGE_HOURS below.
GCAT_CACHE_FILE = os.path.join(BASE_DIR, "data", "gcat_currentcat.tsv")
GCAT_CACHE_MAX_AGE_HOURS = 24

# ── TLE data directory ────────────────────────────────────────────────
# Root folder where ALL persistent TLE data lives: the original
# Space-Track bulk zip files you downloaded, the SQLite history cache,
# the SATCAT / rate-limit logs, and the year-packet zip archives for
# daily/incremental captures.
#
# Set this in your .env file to point at the same folder as your
# downloaded TLE files so everything stays together in one place:
#
#   TLE_DATA_DIR=C:\Users\toddl\OneDrive\Data Science Project\Data\TLEs
#
# If not set, defaults to the tool's own data/ subdirectory so the
# tool still works out of the box with no configuration needed.
# Changing this after the first run just means the tool won't find
# the previously-built cache at the old location -- move the SQLite
# files to the new location or re-import from the zip files.
TLE_DATA_DIR = (
    os.environ.get("TLE_DATA_DIR")
    or os.path.join(BASE_DIR, "data")
)

# Permanent cross-run satellite confidence database. Stores scoring
# results for every satellite ever evaluated so they are never re-computed
# unless the satellite's orbital behavior changes. Lives in TLE_DATA_DIR
# alongside the TLE history cache so all persistent satellite data is
# in one place for backup and portability.
SATELLITE_CONFIDENCE_DB = os.path.join(TLE_DATA_DIR, "satellite_confidence.sqlite3")

# Persistent local cache of Space-Track gp_history (TLE history) data.
# Lives in TLE_DATA_DIR alongside the original bulk zip files so all
# TLE data is in one place for backup and portability.
TLE_HISTORY_CACHE_DB = os.path.join(TLE_DATA_DIR, "tle_history_cache.sqlite3")

# Persistent log of every Space-Track API request made, used by the
# pre-flight policy check to enforce rate limits ACROSS runs.
API_REQUEST_LOG_DB = os.path.join(TLE_DATA_DIR, "api_request_log.sqlite3")

# Local cache of Space-Track SATCAT data, refreshed at most once per
# SATCAT_CACHE_MAX_AGE_HOURS (default 24h, matching Space-Track's
# documented "SATCAT: 1/day" usage policy -- see satcat_cache.py).
SATCAT_CACHE_DB = os.path.join(TLE_DATA_DIR, "satcat_cache.sqlite3")
SATCAT_CACHE_MAX_AGE_HOURS = 24

OUTPUT_FILE = os.path.join(BASE_DIR, "output", "visible_satellites.xlsx")

# Active satellites from CelesTrak
TLE_URL = (
    "https://celestrak.org/NORAD/elements/gp.php?"
    "GROUP=active&FORMAT=tle"
)
