"""
compare_n2yo.py

Spot-check this project's azimuth/elevation calculations against
N2YO's "positions" API for the same satellites, observer location,
and timestamps.

This runs entirely standalone using the satellite catalog you
already have cached locally (data/active.tle) -- it doesn't need
your output spreadsheet at all.

IMPORTANT LIMITATION: N2YO's API only predicts from the current
moment forward (its "positions" endpoint starts at "now" and runs
for up to a few minutes; its pass-prediction endpoints only look
1-10 days ahead). It cannot validate predictions far in the future
(e.g. this project's Aug 5 2026 window, if that's more than ~10
days out from whenever you run this). What this script DOES
validate is the underlying methodology: given the same TLE,
observer location, and timestamp, does this project compute the
same azimuth/elevation as N2YO? That's the part actually worth
checking -- the far-future forecast accuracy limitation applies
equally to N2YO, Heavens-Above, or any other tool using the same
TLE data, so there's nothing further out to "validate" against
even in principle.

SETUP:
  1. Get a free API key at https://www.n2yo.com/api/
     (Create an account -> Profile page -> generate API key)
  2. Add it to your .env file:
       N2YO_API_KEY=your-key-here
  3. Run:
       python compare_n2yo.py

Heavens-Above is NOT included here because it has no public API --
comparing against it has to be done manually on their website
(heavens-above.com), entering your location and checking pass
predictions for the same satellites/times this script reports.
"""

import os
import time
from datetime import datetime, timezone

import requests
from skyfield.api import wgs84

from config import SENSOR_LAT, SENSOR_LON, SENSOR_ELEV_M, TLE_FILE
from satellite_utils import load_satellites


N2YO_API_KEY = os.environ.get("N2YO_API_KEY")
N2YO_BASE = "https://api.n2yo.com/rest/v1/satellite"

# ── N2YO usage policy ────────────────────────────────────────────────────────
#
# N2YO's free tier allows 1,000 transactions per hour PER ENDPOINT CATEGORY
# (https://www.n2yo.com/api/). This script uses only /positions, so its
# budget is 1,000/hour on its own. One run = NUM_SATELLITES_TO_CHECK
# requests (one per satellite; the offsets come back inside a single
# response), so the default of 5 is negligible.
#
# Every N2YO response carries info.transactionscount - our usage this hour,
# reported by them. It was being discarded. It is now read, displayed, and
# used to stop before the cap rather than discovering the limit by hitting
# it. This project has had an API account suspended before (see
# src/tracking/spacetrack_policy_check.py); being told our own usage and
# ignoring it would be careless.
N2YO_HOURLY_LIMIT = 1000        # per endpoint category, free tier
N2YO_SAFETY_MARGIN = 100        # stop this far short of the limit
REQUEST_SPACING_SEC = 1         # courtesy delay between satellites

# How many satellites to spot-check, and how many seconds of
# position data to request from N2YO for each (matched against
# this project's own calculation at the same offsets).
NUM_SATELLITES_TO_CHECK = 5
CHECK_OFFSETS_SEC = [0, 60, 120]

#: Refuse to run above this. This is a spot-check for methodology, not a
#: bulk data source - if it ever needs hundreds of satellites, the right
#: answer is a different approach, not a bigger number here.
MAX_SATELLITES_ALLOWED = 25


def get_n2yo_positions(norad_id, lat, lon, alt_m, seconds, timeout_sec=30):
    """
    Query N2YO's positions endpoint for one satellite, returning
    a list of position dicts (one per second) covering `seconds`
    seconds starting from the current moment.

    Each dict contains at minimum: timestamp (Unix epoch),
    satlatitude, satlongitude, sataltitude, azimuth, elevation,
    ra, dec, and eclipsed fields. The azimuth and elevation values
    are topocentric (referenced to the observer at lat/lon/alt_m),
    which is what we compare against this project's own calculation.

    Raises RuntimeError if N2YO returns an empty or error response,
    so the caller can log the failure and continue to the next
    satellite rather than crashing on a single bad lookup.
    """
    url = (
        f"{N2YO_BASE}/positions/{norad_id}/{lat}/{lon}/{alt_m}/{seconds}/"
        f"&apiKey={N2YO_API_KEY}"
    )
    response = requests.get(url, timeout=timeout_sec)
    response.raise_for_status()
    data = response.json()

    # N2YO reports our usage for the current hour in every response.
    used = (data.get("info") or {}).get("transactionscount")
    if used is not None:
        remaining = N2YO_HOURLY_LIMIT - used
        if remaining <= N2YO_SAFETY_MARGIN:
            raise RuntimeError(
                f"Stopping: {used} of {N2YO_HOURLY_LIMIT} N2YO transactions "
                f"used this hour, only {remaining} left. Wait for the hourly "
                f"reset rather than risking the account."
            )

    if "positions" not in data or not data["positions"]:
        raise RuntimeError(
            data.get("error", "N2YO returned no position data for this satellite.")
        )

    return data["positions"], used


def main():
    """
    Spot-check this project's azimuth/elevation calculations against
    N2YO's live positions API for the same satellites, observer
    location, and timestamps.

    Loads the first NUM_SATELLITES_TO_CHECK satellites from the
    locally cached TLE file (data/active.tle), computes their
    topocentric azimuth and elevation at "now" and at +60s/+120s
    using this project's own Skyfield/SGP4 implementation, then
    queries N2YO for the same values at the same offsets. Prints a
    side-by-side comparison showing the delta between the two, which
    should typically be a fraction of a degree for a recently-
    downloaded catalog.

    This validates methodology (same TLE + location + time ->
    same answer), not the accuracy of long-range future predictions,
    since N2YO's API is also limited to the current catalog and
    can't predict further into the future than this project can.
    """

    if not N2YO_API_KEY:
        raise RuntimeError(
            "N2YO_API_KEY is not set. Add it to your .env file -- get a "
            "free key at https://www.n2yo.com/api/"
        )

    if NUM_SATELLITES_TO_CHECK > MAX_SATELLITES_ALLOWED:
        raise RuntimeError(
            f"NUM_SATELLITES_TO_CHECK is {NUM_SATELLITES_TO_CHECK}, above the "
            f"{MAX_SATELLITES_ALLOWED} cap. This is a methodology spot-check, "
            f"not a bulk source - one request per satellite goes to N2YO."
        )

    print("Loading locally cached satellite catalog...")
    satellites, ts = load_satellites(TLE_FILE)

    observer = wgs84.latlon(SENSOR_LAT, SENSOR_LON, elevation_m=SENSOR_ELEV_M)

    sample = satellites[:NUM_SATELLITES_TO_CHECK]

    now = datetime.now(timezone.utc)
    max_offset = max(CHECK_OFFSETS_SEC)

    print(
        f"\nComparing {len(sample)} satellites against N2YO at "
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"(observer: {SENSOR_LAT}, {SENSOR_LON})\n"
    )

    for sat in sample:

        satnum = sat.model.satnum
        print(f"--- {sat.name} (NORAD {satnum}) ---")

        try:
            n2yo_positions, used = get_n2yo_positions(
                satnum, SENSOR_LAT, SENSOR_LON, SENSOR_ELEV_M, max_offset + 1
            )
            if used is not None:
                print(f"  (N2YO transactions used this hour: {used} of "
                      f"{N2YO_HOURLY_LIMIT})")
        except RuntimeError as e:
            # A budget stop is not a per-satellite failure - stop the run.
            if "Stopping:" in str(e):
                print(f"\n  {e}\n")
                return
            print(f"  N2YO lookup failed: {e}\n")
            continue
        except Exception as e:
            print(f"  N2YO lookup failed: {e}\n")
            continue

        for offset in CHECK_OFFSETS_SEC:

            # --- This project's own calculation ---
            check_time = now.timestamp() + offset
            t = ts.utc(
                datetime.fromtimestamp(check_time, tz=timezone.utc)
            )

            difference = sat - observer
            topocentric = difference.at(t)
            altitude, azimuth, distance = topocentric.altaz()

            our_az = azimuth.degrees
            our_el = altitude.degrees

            # --- N2YO's value at the matching second offset ---
            n2yo_entry = next(
                (p for p in n2yo_positions
                 if abs(p["timestamp"] - check_time) < 1.5),
                None
            )

            if n2yo_entry is None:
                print(f"  +{offset:>3}s: no matching N2YO timestamp returned")
                continue

            n2yo_az = n2yo_entry["azimuth"]
            n2yo_el = n2yo_entry["elevation"]

            print(
                f"  +{offset:>3}s -- "
                f"This project: Az {our_az:7.2f} deg, El {our_el:6.2f} deg  |  "
                f"N2YO: Az {n2yo_az:7.2f} deg, El {n2yo_el:6.2f} deg  |  "
                f"Delta: Az {abs(our_az - n2yo_az):5.2f} deg, "
                f"El {abs(our_el - n2yo_el):5.2f} deg"
            )

        print()

        # Courtesy delay; the free tier allows 1,000/hour but there is no
        # reason to approach it for a five-satellite spot-check.
        time.sleep(REQUEST_SPACING_SEC)


if __name__ == "__main__":
    main()
