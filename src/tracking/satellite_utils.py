"""
satellite_utils.py

Core utility functions for the satellite visibility pipeline.
Organised into six logical sections:

  1. CelesTrak download (download_tle)
       Downloads the live active-satellite TLE catalog from
       CelesTrak, with caching (reuses the local file if it's
       younger than the 2-hour refresh window) and throttle-notice
       detection (CelesTrak returns a plain-text notice instead of
       an HTTP error when re-requested too soon).

  2. Space-Track download (download_tle_spacetrack,
                           spacetrack_login,
                           SpaceTrackRateLimiter)
       Downloads historical TLEs from Space-Track for a specific
       past date, for re-running an analysis after the fact.
       Includes a thread-safe sliding-window rate limiter that
       enforces Space-Track's published limits across concurrent
       batch requests (used by historical_accuracy.py).

  3. Orbital element extraction (parse_tle_orbital_elements,
                                  _parse_tle_epoch)
       Reads orbital elements from raw TLE text columns directly,
       without constructing a full EarthSatellite/SGP4 object.
       Used by the historical batch fetch for fast bulk analysis.

  4. Historical TLE batch fetch (get_historical_orbital_elements_batch,
                                  get_historical_orbital_elements_batch_with_retry,
                                  get_historical_orbital_elements)
       Queries Space-Track's gp_history class for many NORAD IDs at
       once, with automatic retry-by-splitting on timeout and even
       subsampling for densely-tracked objects.

  5. Catalog comparison (extract_tle_records)
       Lightweight TLE file parser returning a plain dict of
       {norad_id: (line1, line2)} for fast before/after catalog
       diffing (used by catalog_diff.py).

  6. Satellite loading and classification (load_satellites,
                                           classify_orbit,
                                           in_sensor_field_of_regard)
       Loads a TLE file into Skyfield EarthSatellite objects,
       classifies each by orbit type, and applies the sensor's
       field-of-regard constraints to a visibility mask.
"""

import os
import time
import math
import threading
from datetime import datetime, timedelta

import numpy as np
import requests

from skyfield.api import load
from skyfield.api import EarthSatellite


# =====================================================
# DOWNLOAD TLE FILE (CelesTrak - live catalog)
# =====================================================

# Text CelesTrak returns instead of data when you request the same
# group again before its 2-hour update window has passed.
_CELESTRAK_THROTTLE_MARKER = "has not updated since your last successful"


def download_tle(url, filename, timeout_sec=30, max_cache_age_hours=2):
    """
    Download active satellite catalog.

    FIX: CelesTrak only refreshes GP data every 2 hours. Requesting
    the same group again sooner doesn't return an HTTP error -- it
    returns a 200 response with a plain-text throttle notice
    ("GP data has not updated since your last successful
    download..."). The original code wrote that notice straight
    into the TLE file, silently corrupting it. This version:

      1. Skips the request entirely and reuses the existing file if
         it's younger than max_cache_age_hours (CelesTrak explicitly
         asks users not to poll faster than this anyway).
      2. Detects the throttle notice if it does come back, and
         falls back to the existing cached file instead of
         overwriting it -- or raises a clear error if no cached
         file exists yet.

    Also sends a descriptive User-Agent identifying this as a
    script rather than the default "python-requests/x.y.z" string,
    which some sites treat as anonymous bot traffic and block
    outright (separate from any rate-limit throttling).
    """

    if os.path.exists(filename):
        age_hours = (time.time() - os.path.getmtime(filename)) / 3600
        if age_hours < max_cache_age_hours:
            print(
                f"Using cached catalog ({age_hours:.1f}h old) -- "
                f"CelesTrak only updates every {max_cache_age_hours}h, "
                f"so no need to re-download yet."
            )
            return

    print("Downloading satellite catalog...")

    headers = {
        "User-Agent": (
            "satellite-visibility-script/1.0 "
            "(personal research project; contact via Space-Track account)"
        )
    }

    response = requests.get(url, headers=headers, timeout=timeout_sec)

    response.raise_for_status()

    if _CELESTRAK_THROTTLE_MARKER in response.text:
        if os.path.exists(filename):
            print(
                "CelesTrak returned a throttle notice instead of data "
                "(too soon since the last successful download). "
                "Reusing the existing cached file."
            )
            return
        else:
            raise RuntimeError(
                "CelesTrak returned a throttle notice and there is no "
                "cached catalog file to fall back on. Wait until at "
                "least 2 hours have passed since your last successful "
                "download, or set DATA_SOURCE = 'spacetrack' in "
                "config.py to use Space-Track instead."
            )

    os.makedirs(os.path.dirname(filename), exist_ok=True)

    with open(filename, "w") as f:
        f.write(response.text)

    print("Catalog downloaded successfully.")


# =====================================================
# DOWNLOAD TLE FILE (Space-Track - historical catalog)
# =====================================================

SPACETRACK_LOGIN_URL = "https://www.space-track.org/ajaxauth/login"
SPACETRACK_QUERY_BASE = "https://www.space-track.org/basicspacedata/query"


class SpaceTrackRateLimiter:
    """
    Thread-safe sliding-window rate limiter enforcing Space-Track's
    published usage limits: fewer than 30 requests per minute and
    fewer than 300 requests per hour. Multiple threads can share one
    instance -- each call to acquire() blocks the calling thread
    until it's safe to send a request, so concurrent batch fetches
    naturally pace themselves against the same shared budget instead
    of each thread tracking its own (and collectively exceeding the
    real limit).

    Defaults are set slightly under the documented limits (28/min,
    290/hour) as a safety margin.

    log_callback: optional callable(request_class, norad_count) that
    is called immediately after a slot is granted (i.e. a real
    request is about to be made). Used to write to the persistent
    cross-run api_request_log so the pre-flight check can enforce
    rate limits across separate program invocations, not just within
    one run. If None (default) logging is skipped; set this at
    construction time in historical_accuracy.py after the rate
    limiter is created.
    """

    def __init__(self, per_minute=28, per_hour=290, log_callback=None):
        self.per_minute    = per_minute
        self.per_hour      = per_hour
        self.lock          = threading.Lock()
        self.minute_window = []
        self.hour_window   = []
        self.log_callback  = log_callback

    def acquire(self, request_class="other", norad_count=0):
        """
        Block the calling thread until a request slot is available
        under both the per-minute and per-hour budgets, then
        claim one slot and return. Thread-safe -- multiple threads
        can call this concurrently and will serialize safely through
        the internal lock without any external coordination needed.

        request_class: one of the api_request_log.CLASS_* constants
        ("gp_history", "satcat", "login", "other") -- used purely
        for logging; doesn't affect rate-limit math.
        norad_count: how many NORAD IDs this request covers --
        informational, logged for diagnostic purposes.
        """
        while True:
            with self.lock:
                now = time.time()
                # Evict timestamps that have aged out of each window
                # before checking whether a slot is free.
                self.minute_window = [t for t in self.minute_window if now - t < 60]
                self.hour_window = [t for t in self.hour_window if now - t < 3600]

                if (
                    len(self.minute_window) < self.per_minute
                    and len(self.hour_window) < self.per_hour
                ):
                    self.minute_window.append(now)
                    self.hour_window.append(now)
                    if self.log_callback is not None:
                        try:
                            self.log_callback(request_class, norad_count)
                        except Exception:
                            pass  # never let logging break the actual request
                    return

            # Re-check periodically rather than computing one long
            # sleep, since other threads may free up slots sooner
            # (a 0.5s poll is accurate enough given the ~2s/request
            # pacing we're targeting and avoids busy-looping).
            time.sleep(0.5)


_LAST_LOGIN_TIME = [0.0]   # mutable singleton so it persists across calls
_LOGIN_MIN_INTERVAL_SEC = 60   # never log in more than once per minute

_ACCOUNT_DIAGNOSIS_SHOWN = [False]   # show the detailed inactive-account
                                      # explanation once per process, not
                                      # once per batch (every batch will
                                      # fail identically if the account
                                      # really is inactive/suspended)

def spacetrack_session_is_valid(session, timeout_sec=15):
    """
    Verify that a Space-Track session cookie is still valid using the
    officially documented /app/data/whoami endpoint.

    Per Space-Track's own How-To documentation:
      "To check if your current cookie is still valid and extend its
      lifetime by another 2 hours, make a GET request to
      https://www.space-track.org/app/data/whoami. This returns a
      JSON object with: logged_in (bool), identity (username or null),
      session_expiration (ISO-8601 timestamp)."

    This is the CORRECT, officially documented way to check session
    validity -- it's a lightweight metadata endpoint that explicitly
    exists for this purpose, costs nothing against any data class
    rate limits, and extends the session by 2 hours as a side effect.

    Previous versions used class/satcat/limit/1 as a probe, which
    violated the documented SATCAT 1/day limit by spending one of
    those requests just to check authentication status. The whoami
    endpoint has no such per-class usage restriction.

    Returns True if logged_in=True, False otherwise (including any
    network error -- treat unknown as invalid to force re-auth rather
    than risk cascading silent 401s).
    """
    whoami_url = "https://www.space-track.org/app/data/whoami"
    try:
        resp = session.get(whoami_url, timeout=timeout_sec)
        if resp.status_code != 200:
            return False
        import json as _json
        data = _json.loads(resp.text)
        return bool(data.get("logged_in", False))
    except Exception:
        return False


def ensure_spacetrack_session(session, username, password, rate_limiter=None,
                               timeout_sec=15):
    """
    Verify the given session is still authenticated using the
    /app/data/whoami endpoint (which Space-Track documents explicitly
    for this purpose and which costs nothing against any data class
    rate limit). If the session has expired, transparently re-login
    and return the new session.

    This was previously a no-op pass-through because the earlier
    implementation checked validity by querying class/satcat/limit/1,
    which violated the documented SATCAT 1/day limit. Now that
    spacetrack_session_is_valid() uses the correct /app/data/whoami
    endpoint, this function can do real session checking again at
    zero policy cost -- the whoami endpoint is a metadata endpoint
    that extends the session by 2 hours as a side effect, explicitly
    documented for exactly this use case.
    """
    if spacetrack_session_is_valid(session, timeout_sec=timeout_sec):
        return session

    print(
        "  Space-Track session has expired -- re-authenticating...",
        flush=True,
    )
    try:
        session.close()
    except Exception:
        pass
    return spacetrack_login(
        username, password, rate_limiter=rate_limiter, verify_account=False,
    )


def spacetrack_login(username, password, timeout_sec=30, rate_limiter=None,
                      verify_account=True):
    """
    Authenticate with Space-Track and return a requests.Session
    carrying the auth cookie, for use in one or more subsequent
    queries. Reuse the SAME session across multiple queries rather
    than logging in again for each one -- Space-Track enforces its
    own (stricter, separate) rate limit on the login endpoint itself,
    and repeated logins burn through that budget fast.

    rate_limiter: if provided, login also passes through it so the
    login request is counted against the same shared budget used by
    data queries -- this matters because Space-Track tracks total
    request volume across endpoints, not the login endpoint in
    isolation.

    A minimum 60-second interval between logins is also enforced
    process-wide (regardless of which rate_limiter instance is
    passed in), since rapid re-authentication is one of the specific
    patterns Space-Track's abuse detection flags.

    verify_account: if True (default), immediately follows a
    successful login with one minimal real data query to confirm the
    account can actually use the API -- not just that the username/
    password were correct. This catches accounts that are inactive,
    suspended, or otherwise restricted: those can still return a
    normal 200 OK login response with no "Login Failed" text (the
    only two things the login step itself checks), then fail every
    subsequent real query.

    verify_account is accepted for backward compatibility but no
    longer triggers a dedicated verification query -- an earlier
    version of this parameter made one extra real query against the
    SATCAT class immediately after every login specifically to check
    this, which directly violated Space-Track's documented usage
    policy (SATCAT queries are limited to 1 PER DAY; see
    https://www.space-track.org/documentation, "API Use Guidelines").

    With the corrected implementation of spacetrack_session_is_valid()
    now using the documented /app/data/whoami endpoint (which has no
    per-class rate limit and is explicitly documented for session
    checking), verify_account is now active and policy-compliant
    again: a whoami check after login costs nothing against any class
    quota, and correctly catches inactive/suspended accounts before
    the expensive multi-minute scoring pipeline starts.
    """
    import time as _time

    elapsed_since_last = _time.time() - _LAST_LOGIN_TIME[0]
    if elapsed_since_last < _LOGIN_MIN_INTERVAL_SEC:
        wait = _LOGIN_MIN_INTERVAL_SEC - elapsed_since_last
        print(
            f"  Waiting {wait:.0f}s before re-authenticating with Space-Track "
            f"(minimum {_LOGIN_MIN_INTERVAL_SEC}s between logins)..."
        )
        _time.sleep(wait)

    if rate_limiter:
        rate_limiter.acquire(request_class="login")

    session = requests.Session()

    payload = {"identity": username, "password": password}

    response = session.post(
        SPACETRACK_LOGIN_URL,
        data=payload,
        timeout=timeout_sec
    )

    _LAST_LOGIN_TIME[0] = _time.time()

    response.raise_for_status()

    if "Login Failed" in response.text:
        raise RuntimeError(
            "Space-Track login failed. Check SPACETRACK_USERNAME / "
            "SPACETRACK_PASSWORD."
        )

    if verify_account:
        # Use the documented /app/data/whoami endpoint -- free of any
        # per-class rate limit, explicitly exists for this purpose, and
        # extends the session by 2 hours as a side effect. This catches
        # inactive/suspended accounts (which can still return a normal
        # login success response) before the expensive pipeline starts.
        if not spacetrack_session_is_valid(session, timeout_sec=timeout_sec):
            try:
                session.close()
            except Exception:
                pass
            raise RuntimeError(
                "Space-Track login succeeded (credentials were correct) "
                "but the account appears INACTIVE, SUSPENDED, or "
                "RESTRICTED -- the session cookie does not authenticate "
                "against the /app/data/whoami endpoint. Log into "
                "https://www.space-track.org in a browser and check for "
                "a reactivation or re-verification prompt, then re-run."
            )

    return session


def _parse_tle_epoch(line1):
    """
    Parse the epoch field of a TLE line 1 (columns 19-32,
    format YYDDD.DDDDDDDD) into a datetime.
    """

    epoch_str = line1[18:32].strip()

    yy = int(epoch_str[0:2])
    year = 2000 + yy if yy < 57 else 1900 + yy

    day_of_year_frac = float(epoch_str[2:])

    return datetime(year, 1, 1) + timedelta(days=day_of_year_frac - 1)


def parse_tle_orbital_elements(line1, line2):
    """
    Extract orbital elements from TLE text via fixed-column parsing.
    Used for bulk zip file imports which are in 2LE/3LE text format.
    For live Space-Track API responses, use parse_gp_history_json_record
    instead -- JSON avoids Alpha-5 encoding issues for IDs > 99,999.
    """
    MU = 398600.4418
    EARTH_RADIUS_KM = 6378.137

    inclination_deg = float(line2[8:16])
    eccentricity = float("0." + line2[26:33].strip())
    mean_motion_rev_per_day = float(line2[52:63])

    n_rad_per_sec = mean_motion_rev_per_day * 2 * math.pi / 86400
    semi_major_axis_km = (MU / n_rad_per_sec ** 2) ** (1 / 3)
    altitude_km = semi_major_axis_km - EARTH_RADIUS_KM
    period_min = 1440.0 / mean_motion_rev_per_day

    return {
        "epoch": _parse_tle_epoch(line1),
        "altitude_km": altitude_km,
        "inclination_deg": inclination_deg,
        "eccentricity": eccentricity,
        "period_min": period_min,
    }


def parse_gp_history_json_record(rec):
    """
    Parse one JSON record from Space-Track's gp_history JSON response
    into the same orbital element dict shape used throughout the tool.

    JSON format is preferred over 3LE for live API calls because:
    - NORAD_CAT_ID is always an integer -- no Alpha-5 encoding issues
      for satellites with IDs > 99,999 (the TLE format breaks at 69,999,
      estimated to occur around 2026-07-12 with the current catalog
      growth rate from Starlink and other mega-constellations)
    - APOGEE/PERIGEE/PERIOD are pre-calculated by Space-Track --
      no need to derive altitude from mean motion ourselves
    - Fields are named and extensible, not fixed-width columns

    Returns the same dict as parse_tle_orbital_elements():
    {epoch, altitude_km, inclination_deg, eccentricity, period_min}
    """
    from datetime import datetime as _dt

    epoch_str = rec.get("EPOCH", "")
    try:
        # Space-Track JSON EPOCH format: "2024-01-01T12:00:00.000000"
        epoch = _dt.fromisoformat(epoch_str.replace("Z", ""))
    except (ValueError, AttributeError):
        epoch = None

    # Use APOGEE (km above Earth's surface) as altitude proxy.
    # APOGEE = semi_major_axis * (1 + e) - Earth_radius_km
    # For a circular orbit this equals altitude_km directly.
    try:
        apogee = float(rec.get("APOGEE") or 0)
        perigee = float(rec.get("PERIGEE") or 0)
        altitude_km = (apogee + perigee) / 2.0
    except (ValueError, TypeError):
        # Fallback: calculate from mean motion if APOGEE not available
        try:
            mean_motion = float(rec.get("MEAN_MOTION") or 0)
            if mean_motion > 0:
                MU = 398600.4418
                n = mean_motion * 2 * 3.14159265 / 86400
                sma = (MU / n ** 2) ** (1 / 3)
                altitude_km = sma - 6378.137
            else:
                altitude_km = 0
        except (ValueError, TypeError):
            altitude_km = 0

    try:
        period_min = float(rec.get("PERIOD") or 0)
        if period_min == 0:
            mean_motion = float(rec.get("MEAN_MOTION") or 0)
            period_min = 1440.0 / mean_motion if mean_motion > 0 else 0
    except (ValueError, TypeError):
        period_min = 0

    try:
        inclination_deg = float(rec.get("INCLINATION") or 0)
    except (ValueError, TypeError):
        inclination_deg = 0

    try:
        eccentricity = float(rec.get("ECCENTRICITY") or 0)
    except (ValueError, TypeError):
        eccentricity = 0

    return {
        "epoch":           epoch,
        "altitude_km":     altitude_km,
        "inclination_deg": inclination_deg,
        "eccentricity":    eccentricity,
        "period_min":      period_min,
    }



class SpaceTrackMalformedResponseError(RuntimeError):
    """
    Raised when a Space-Track query returns a 200 OK with a non-empty
    body that doesn't parse as the expected data format (e.g. an error
    message, HTML, or other unexpected content instead of TLE lines).

    Distinct from a generic RuntimeError/timeout so that
    get_historical_orbital_elements_batch_with_retry can recognize
    "this batch's response itself was wrong" and skip the normal
    retry-by-splitting behavior -- splitting only helps when a batch
    is too large/slow for Space-Track to assemble in time, which is
    not the case here: a malformed response affects every NORAD ID in
    the batch identically regardless of size, including a batch of
    one. Splitting it down to min_split_size would just repeat the
    exact same failure at every level, burning the retry deadline for
    no benefit, before eventually reporting failure anyway. Failing
    fast here gets to the same correct outcome (report as "Query
    failed") without that wasted time.
    """
    pass


def get_historical_orbital_elements_batch_with_retry(
    session,
    norad_ids,
    start_date,
    end_date,
    timeout_sec=120,
    max_records_per_satellite=2000,
    min_split_size=5,
    rate_limiter=None,
    max_total_seconds=None,
    _deadline=None,
):
    """
    Wraps get_historical_orbital_elements_batch() with automatic
    retry-by-splitting: if a batch query times out or otherwise
    fails (most commonly because the batch is too large/heavy for
    Space-Track to assemble within the timeout), this splits the
    batch in half and retries each half recursively, instead of
    just giving up and silently dropping every satellite in the
    batch from the results.

    Stops splitting once a sub-batch reaches min_split_size; if a
    sub-batch that small still fails, those NORAD IDs are returned
    in the second element of the tuple (failed_norad_ids) so the
    caller can report them as "query failed" rather than silently
    missing or, worse, mislabeled as "no historical data found"
    (which would imply something different -- that Space-Track has
    no records, not that the request itself failed).

    A SpaceTrackMalformedResponseError (the response wasn't even
    TLE-shaped) skips splitting entirely and reports the whole batch
    as failed immediately -- see that class's docstring for why
    splitting can't help with that failure mode. All other
    exceptions (timeouts, connection errors, etc.) still go through
    the normal split-and-retry path, since those genuinely can be
    batch-size-related.

    HARD DEADLINE: each split level retries with the full timeout_sec
    again, and the two halves are tried sequentially (not concurrently),
    so a batch that keeps failing at every split level can take up to
    timeout_sec × (number of split levels) -- with no upper bound,
    a single stuck batch could occupy a worker thread indefinitely
    while the rest of the run waits on it via as_completed(). 
    max_total_seconds caps the TOTAL wall-clock time this call (across
    all recursive retries) is allowed to spend, defaulting to
    4 × timeout_sec. Once the deadline passes, any remaining IDs are
    returned as failed immediately rather than attempting another
    split -- they'll show as "Query failed" and can be retried on
    the next run, rather than stalling this run indefinitely.

    rate_limiter: an optional SpaceTrackRateLimiter shared across
    all concurrent callers, so parallel batch fetches collectively
    respect Space-Track's published rate limits rather than each
    one tracking its own and exceeding the real shared budget.

    Returns (elements_by_norad, failed_norad_ids).
    """
    import time as _time

    # Establish the deadline once, at the top-level call. Recursive
    # calls receive the SAME absolute deadline via _deadline so the
    # cap applies to the whole original batch's total retry time,
    # not separately to each split.
    if _deadline is None:
        if max_total_seconds is None:
            max_total_seconds = timeout_sec * 4
        _deadline = _time.time() + max_total_seconds

    if _time.time() >= _deadline:
        # Deadline already passed (e.g. an earlier sibling split used
        # up the whole budget) -- don't attempt another network call,
        # just report these IDs as failed so the caller isn't blocked
        # any further.
        return {}, list(norad_ids)

    try:
        elements_by_norad = get_historical_orbital_elements_batch(
            session,
            norad_ids,
            start_date,
            end_date,
            timeout_sec=timeout_sec,
            max_records_per_satellite=max_records_per_satellite,
            rate_limiter=rate_limiter
        )
        return elements_by_norad, []

    except SpaceTrackMalformedResponseError as e:
        # Fail fast -- see SpaceTrackMalformedResponseError's docstring.
        # Surface this loudly (not just per-satellite) since it usually
        # means something is wrong with the query/session/account, not
        # with these specific satellites, and the same failure will
        # repeat for every other batch in this run too.
        if not _ACCOUNT_DIAGNOSIS_SHOWN[0]:
            _ACCOUNT_DIAGNOSIS_SHOWN[0] = True
            print(
                f"\n  Space-Track returned a malformed/unexpected response "
                f"for {len(norad_ids)} satellite(s): {e}\n"
                f"\n"
                f"  This is the same signature as an INACTIVE, SUSPENDED, "
                f"or otherwise RESTRICTED Space-Track account: login can "
                f"succeed normally while real data queries are rejected. "
                f"Log in directly at https://www.space-track.org in a "
                f"browser with these same credentials and check the "
                f"account status for a reactivation or re-verification "
                f"prompt.\n"
                f"\n"
                f"  If the account IS active, this can also mean the "
                f"query itself was rejected -- Space-Track's gp_history "
                f"class is documented as 1-query-per-object-PER-LIFETIME "
                f"(see https://www.space-track.org/documentation, 'API "
                f"Use Guidelines'); repeatedly re-querying full history "
                f"for the same satellites across many runs is a usage-"
                f"policy violation that can itself trigger suspension. "
                f"This same failure will repeat for every remaining "
                f"batch in this run -- only this first occurrence is "
                f"shown in full detail.\n",
                flush=True,
            )
        else:
            print(
                f"  Space-Track query failed for {len(norad_ids)} "
                f"satellite(s) (same cause as above).",
                flush=True,
            )
        return {}, list(norad_ids)

    except Exception as e:

        if len(norad_ids) <= min_split_size:
            # Already as small as we'll go -- this batch genuinely
            # failed, report it as such rather than retrying forever.
            return {}, list(norad_ids)

        if _time.time() >= _deadline:
            # The failed attempt above already used up the remaining
            # budget -- stop here instead of starting a split that
            # would just immediately hit the deadline check anyway.
            return {}, list(norad_ids)

        mid = len(norad_ids) // 2
        first_half, second_half = norad_ids[:mid], norad_ids[mid:]

        results_a, failed_a = get_historical_orbital_elements_batch_with_retry(
            session, first_half, start_date, end_date,
            timeout_sec=timeout_sec,
            max_records_per_satellite=max_records_per_satellite,
            min_split_size=min_split_size,
            rate_limiter=rate_limiter,
            _deadline=_deadline,
        )

        # Check again before starting the second half -- if the first
        # half consumed the rest of the budget, don't attempt the
        # second half's network call either.
        if _time.time() >= _deadline:
            return results_a, failed_a + list(second_half)

        results_b, failed_b = get_historical_orbital_elements_batch_with_retry(
            session, second_half, start_date, end_date,
            timeout_sec=timeout_sec,
            max_records_per_satellite=max_records_per_satellite,
            min_split_size=min_split_size,
            rate_limiter=rate_limiter,
            _deadline=_deadline,
        )

        merged = {**results_a, **results_b}
        return merged, failed_a + failed_b


def get_historical_orbital_elements_batch(
    session,
    norad_ids,
    start_date,
    end_date,
    timeout_sec=30,
    max_records_per_satellite=2000,
    rate_limiter=None
):
    """
    Fetch historical TLE epochs for MULTIPLE satellites in a single
    Space-Track query, using a comma-delimited NORAD_CAT_ID list.

    Space-Track's own usage policy explicitly asks users not to send
    one query per satellite for things like this, and to combine
    multiple objects into a comma-delimited list instead -- this is
    both dramatically faster (one request instead of N) and the
    approach they actually want used. See:
    https://www.space-track.org/documentation -> "Recommended Queries"

    Returns a dict: {norad_id (int): [orbital element dicts, ...]},
    time-ordered per satellite. Any norad_id with zero matching
    records simply won't be a key in the result (caller should treat
    a missing key the same as "no historical data found").
    """

    id_list = ",".join(str(n) for n in norad_ids)
    epoch_range = f"{start_date.isoformat()}--{end_date.isoformat()}"

    # Use JSON format instead of 3LE. This is required for NORAD IDs
    # > 99,999 (the 5-digit TLE format runs out at 69,999 around
    # 2026-07-12; after that new satellites are only available in
    # JSON/XML/CSV formats). JSON also gives us APOGEE/PERIOD/PERIGEE
    # fields directly rather than calculating them from mean motion.
    query_url = (
        f"{SPACETRACK_QUERY_BASE}/class/gp_history/NORAD_CAT_ID/{id_list}"
        f"/EPOCH/{epoch_range}/orderby/NORAD_CAT_ID,EPOCH/format/json"
        f"/emptyresult/show"
    )

    if rate_limiter is not None:
        rate_limiter.acquire(
            request_class="gp_history",
            norad_count=len(norad_ids),
        )

    response = session.get(query_url, timeout=timeout_sec)
    response.raise_for_status()

    response_text = response.text.strip()

    # Parse JSON response
    import json as _json
    by_norad = {}
    if response_text and response_text != "NO RESULTS RETURNED":
        try:
            records_json = _json.loads(response_text)
        except _json.JSONDecodeError:
            raise SpaceTrackMalformedResponseError(
                f"gp_history query returned non-JSON response. "
                f"First 200 chars: {response_text[:200]!r}"
            )
        if not isinstance(records_json, list):
            raise SpaceTrackMalformedResponseError(
                f"gp_history JSON response was not a list: "
                f"{response_text[:200]!r}"
            )
        for rec in records_json:
            try:
                norad_id = int(rec["NORAD_CAT_ID"])
                by_norad.setdefault(norad_id, []).append(rec)
            except (KeyError, ValueError):
                continue

    if response_text and response_text != "NO RESULTS RETURNED" and not by_norad:
        raise SpaceTrackMalformedResponseError(
            f"gp_history query for {len(norad_ids)} satellite(s) returned "
            f"a non-empty response that contained no parseable records. "
            f"First 200 chars: {response_text[:200]!r}"
        )

    result = {}
    for record_norad_id, records in by_norad.items():

        if len(records) > max_records_per_satellite:
            step = len(records) / max_records_per_satellite
            records = [
                records[int(j * step)] for j in range(max_records_per_satellite)
            ]

        elements = []
        for rec in records:
            try:
                elements.append(parse_gp_history_json_record(rec))
            except Exception:
                continue

        elements = [e for e in elements if e.get("epoch") is not None]
        elements.sort(key=lambda e: e["epoch"])
        result[record_norad_id] = elements

    return result


def get_historical_orbital_elements(
    session,
    norad_id,
    start_date,
    end_date,
    timeout_sec=30,
    max_records=2000
):
    """
    Fetch every TLE epoch Space-Track has on file for a single
    satellite within [start_date, end_date], and return a
    time-ordered list of orbital element dicts (see
    parse_tle_orbital_elements). Kept for single-satellite use
    cases; historical_accuracy.py uses the batch version instead
    for efficiency across many satellites.

    Takes an already-authenticated session (from spacetrack_login())
    so that evaluating many satellites only requires one login, not
    one per satellite.

    If more than max_records TLEs are returned (common for actively
    tracked LEO objects updated multiple times a day over a 10-year
    window), this subsamples evenly across the range rather than
    processing every single one -- the goal here is to characterize
    long-term drift/stability, not to reconstruct every maneuver,
    so even sampling is a reasonable tradeoff for speed.
    """

    epoch_range = f"{start_date.isoformat()}--{end_date.isoformat()}"

    query_url = (
        f"{SPACETRACK_QUERY_BASE}/class/gp_history/NORAD_CAT_ID/{norad_id}"
        f"/EPOCH/{epoch_range}/orderby/EPOCH/format/json/emptyresult/show"
    )

    response = session.get(query_url, timeout=timeout_sec)
    response.raise_for_status()

    import json as _json
    response_text = response.text.strip()
    records = []
    if response_text and response_text != "NO RESULTS RETURNED":
        try:
            records = _json.loads(response_text)
            if not isinstance(records, list):
                records = []
        except _json.JSONDecodeError:
            raise SpaceTrackMalformedResponseError(
                f"gp_history query for NORAD {norad_id} returned non-JSON -- "
                f"first 200 chars: {response_text[:200]!r}"
            )

    if response_text and response_text != "NO RESULTS RETURNED" and not records:
        raise SpaceTrackMalformedResponseError(
            f"gp_history query for NORAD {norad_id} returned non-empty "
            f"response with no parseable records -- first 200 chars: "
            f"{response_text[:200]!r}"
        )

    if len(records) > max_records:
        step = len(records) / max_records
        records = [
            records[int(j * step)] for j in range(max_records)
        ]

    elements = []
    for rec in records:
        try:
            elements.append(parse_gp_history_json_record(rec))
        except Exception:
            continue

    elements = [e for e in elements if e.get("epoch") is not None]
    elements.sort(key=lambda e: e["epoch"])

    return elements


def download_tle_spacetrack(
    target_date,
    filename,
    username,
    password,
    window_days=3,
    timeout_sec=30
):
    """
    Download historical TLEs from Space-Track, keeping the single
    epoch per satellite closest to target_date.

    Unlike CelesTrak's live feed (which only ever reflects "right
    now"), this lets you reproduce an analysis for a specific past
    date. Space-Track's gp_history class returns every epoch a
    satellite had within the requested date range, so this
    function queries a window around target_date and then filters
    down to the single nearest epoch per NORAD ID.

    NOTE ON USAGE POLICY: Space-Track documents gp_history as
    "1 query per object per lifetime" -- this function is NOT wired
    into the persistent local cache used by historical_accuracy.py's
    main confidence-scoring pipeline (see tle_history_cache.py),
    since it serves a different purpose (one TLE snapshot near a
    specific past date, not ongoing multi-year history per
    satellite) and is normally called manually/infrequently rather
    than as part of an automated repeated pipeline. Even so: the
    output `filename` this writes IS the local copy you're supposed
    to keep and reuse -- don't call this again for the same
    target_date if you already have the file. If you need to
    reproduce the same past date repeatedly, save and reuse the
    output file rather than re-running this function.

    target_date: a datetime or date for the day you want elements
                 valid near.
    """

    # Accept either a date or a datetime for target_date.
    if hasattr(target_date, "hour"):
        target_date_only = target_date.date()
    else:
        target_date_only = target_date

    start = target_date_only - timedelta(days=window_days)
    end = target_date_only + timedelta(days=window_days)

    epoch_range = f"{start.isoformat()}--{end.isoformat()}"

    query_url = (
        f"{SPACETRACK_QUERY_BASE}/class/gp_history/EPOCH/{epoch_range}"
        f"/orderby/NORAD_CAT_ID,EPOCH/format/3le/emptyresult/show"
    )

    print(
        f"Querying Space-Track for TLEs near {target_date_only.isoformat()} "
        f"(window: {start.isoformat()} to {end.isoformat()})..."
    )

    session = spacetrack_login(username, password, timeout_sec=timeout_sec)

    try:
        response = session.get(query_url, timeout=timeout_sec)
        response.raise_for_status()
    finally:
        session.close()

    raw_lines = [
        line for line in response.text.split("\n") if line.strip() != ""
    ]

    # Group raw text into (name, line1, line2) records using the
    # same marker-based scan as load_satellites(), so this stays
    # robust to blank lines / missing name lines in the response.
    records = []
    i = 0
    n = len(raw_lines)

    while i < n:
        line = raw_lines[i]

        if line.startswith("1 ") and i + 1 < n and raw_lines[i + 1].startswith("2 "):
            name = raw_lines[i - 1].strip() if i > 0 else "UNKNOWN"
            records.append((name, line, raw_lines[i + 1]))
            i += 2
            continue

        i += 1

    if not records:
        response_text = response.text.strip()
        if response_text:
            # Non-empty response that still produced zero parseable
            # records -- almost certainly an error/malformed response
            # rather than genuinely "nothing in this window." See
            # SpaceTrackMalformedResponseError's docstring for the
            # broader context on why this distinction matters.
            raise SpaceTrackMalformedResponseError(
                f"Space-Track returned a non-empty response with no "
                f"parseable TLE records for the window "
                f"{start.isoformat()} to {end.isoformat()} -- this "
                f"usually means an error message or rejected query, "
                f"not genuinely empty data. First 200 chars of "
                f"response: {response_text[:200]!r}"
            )
        raise RuntimeError(
            "Space-Track returned no TLE records for the requested "
            "window. Try a larger SPACETRACK_WINDOW_DAYS, or confirm "
            "the account has gp_history access."
        )

    # Keep only the epoch closest to target_date per satellite.
    target_dt = datetime(
        target_date_only.year, target_date_only.month, target_date_only.day
    )

    best_by_norad = {}

    for name, line1, line2 in records:
        try:
            norad_id = line1[2:7].strip()
            epoch_dt = _parse_tle_epoch(line1)
            delta = abs((epoch_dt - target_dt).total_seconds())
        except Exception:
            continue

        # Keep whichever epoch for this NORAD ID has the smallest
        # absolute time difference from target_date. Space-Track
        # returns every epoch in the window (not just one per
        # satellite), so this dedup step is what gives us a clean
        # one-TLE-per-satellite output file.
        existing = best_by_norad.get(norad_id)
        if existing is None or delta < existing[0]:
            best_by_norad[norad_id] = (delta, name, line1, line2)

    os.makedirs(os.path.dirname(filename), exist_ok=True)

    with open(filename, "w") as f:
        for _, name, line1, line2 in best_by_norad.values():
            f.write(f"{name}\n{line1}\n{line2}\n")

    print(
        f"Saved {len(best_by_norad)} satellites "
        f"(nearest available epoch to {target_date_only.isoformat()} each)."
    )



def fetch_satcat_data(session, norad_ids, timeout_sec=60, rate_limiter=None,
                       username=None, password=None, _retried=False):
    """
    Query Space-Track's SATCAT (satellite catalog) endpoint for a
    list of NORAD IDs and return a dict mapping each NORAD ID to a
    small metadata dict.

    The SATCAT is the authoritative source of:
      launch_date     -- YYYY-MM-DD (no time; launch time is not in
                         any public catalog)
      intl_designator -- e.g. "1998-067A" where 1998 = launch year,
                         067 = 67th launch that year, A = primary payload
      object_type     -- PAYLOAD / ROCKET BODY / DEBRIS / UNKNOWN
      country         -- two-letter country code of launching nation
      launch_site     -- launch site code (e.g. TYMSC = Baikonur)
      size_class      -- SMALL / MEDIUM / LARGE (approximate, from
                         radar cross-section)
      decay_date      -- YYYY-MM-DD if the object has reentered,
                         blank if still in orbit

    Called once per run after scoring is complete, in large batches
    (up to 500 IDs per request) since SATCAT records are tiny compared
    to TLE history.

    username/password: if provided, enables a one-time automatic
    retry on 401 Unauthorized -- the caller's session is re-logged-in
    and the SATCAT query is retried exactly once before giving up.
    This is defense-in-depth alongside the caller checking session
    validity upfront (ensure_spacetrack_session): that upfront check
    handles the common case (session expired from a long-running pass
    elsewhere in the pipeline), but if a session somehow still fails
    at the moment of the actual query -- a race, a transient server-
    side invalidation, etc. -- this retries instead of silently
    returning empty launch metadata for the whole batch. If username/
    password aren't provided, behaves exactly as before (single
    attempt, returns {} on any failure).

    Returns {} on complete failure so the caller can carry on without
    crashing.
    """
    if not norad_ids:
        return {}

    ids_str = ",".join(str(int(n)) for n in norad_ids)
    url = (
        f"{SPACETRACK_QUERY_BASE}/class/satcat/"
        f"NORAD_CAT_ID/{ids_str}/"
        f"format/json"
    )

    if rate_limiter:
        rate_limiter.acquire(
            request_class="satcat",
            norad_count=len(norad_ids),
        )

    try:
        response = session.get(url, timeout=timeout_sec)
        if response.status_code == 401 and username and password and not _retried:
            print(
                "  SATCAT query got 401 Unauthorized despite an earlier "
                "session check -- re-authenticating and retrying this "
                "batch once...",
                flush=True,
            )
            try:
                session.close()
            except Exception:
                pass
            fresh_session = spacetrack_login(
                username, password, rate_limiter=rate_limiter
            )
            # Note: the retried call reuses the SAME fresh_session for
            # this batch; the caller is responsible for picking up the
            # new session for subsequent batches too (fetch_satcat_data
            # has no way to mutate the caller's session reference, so
            # callers that loop over many batches should prefer
            # ensure_spacetrack_session upfront and treat this retry as
            # a last-resort safety net, not the primary mechanism).
            return fetch_satcat_data(
                fresh_session, norad_ids, timeout_sec=timeout_sec,
                rate_limiter=rate_limiter, username=username,
                password=password, _retried=True,
            )
        response.raise_for_status()
        records  = response.json()
    except Exception as e:
        print(f"  SATCAT query failed: {e} -- launch data will be blank.")
        return {}

    result = {}
    for rec in records:
        try:
            norad = int(rec.get("NORAD_CAT_ID") or rec.get("OBJECT_NUMBER") or 0)
            if not norad:
                continue
            result[norad] = {
                "launch_date":     rec.get("LAUNCH", "")     or "",
                "intl_designator": rec.get("INTLDES", "")    or rec.get("OBJECT_ID", "") or "",
                "object_type":     rec.get("OBJECT_TYPE", "") or "",
                "country":         rec.get("COUNTRY", "")    or "",
                "launch_site":     rec.get("SITE", "")       or "",
                "size_class":      rec.get("RCS_SIZE", "")   or "",
                "decay_date":      rec.get("DECAY", "")      or "",
            }
        except Exception:
            pass

    return result



def fetch_gcat_catalog(cache_path, max_cache_age_hours=24, timeout_sec=60):
    """
    Download (or reuse a cached copy of) Jonathan McDowell's GCAT
    (General Catalog of Artificial Space Objects) and return a dict
    mapping NORAD ID -> metadata.

    GCAT is a community-maintained, freely-licensed (CC-BY) catalog
    that is independent of Space-Track and often has richer owner/
    operator and status information than the official SATCAT --
    useful as a secondary source to fill gaps, not a replacement.
    See https://planet4589.org/space/gcat/ for the project itself.

    The full catalog covers every artificial object launched since
    1957 (~70,000+ rows) and is only updated roughly monthly, so it
    is cached to disk and only re-downloaded when the cache is older
    than max_cache_age_hours -- there is no reason to re-fetch a
    ~70,000-row file on every run of this tool.

    Returns a dict: {norad_id: {"owner": str, "state": str,
    "status": str, "ldate": str}}. Returns {} on any failure so the
    caller can continue without GCAT data rather than crashing --
    this is a supplementary source, not a required one.
    """
    import os as _os
    import time as _time

    url = "https://planet4589.org/space/gcat/tsv/derived/currentcat.tsv"

    use_cache = (
        os.path.exists(cache_path)
        and (_time.time() - _os.path.getmtime(cache_path)) < max_cache_age_hours * 3600
    )

    if use_cache:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            use_cache = False

    if not use_cache:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; SatelliteVisibilityTool/1.0; "
                    "+https://github.com/) Python-requests"
                )
            }
            response = requests.get(url, timeout=timeout_sec, headers=headers)
            response.raise_for_status()
            text = response.text
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"  GCAT catalog fetch failed: {e} -- skipping GCAT cross-reference.")
            return {}

    result = {}
    lines = text.splitlines()

    # GCAT's TSV conventionally starts with one or more '#'-prefixed
    # comment lines, sometimes including a column-name header. Capture
    # it for diagnostics -- if the column layout below ever turns out
    # to be wrong (McDowell's GCAT format has changed column order/
    # count before), this is the fastest way to see what's actually
    # in the file without re-downloading and inspecting it by hand.
    header_lines = [line for line in lines[:5] if line.startswith("#")]

    parsed_count = 0
    skipped_short_count = 0
    skipped_blank_id_count = 0

    for line in lines:
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 13:
            skipped_short_count += 1
            continue

        satcat_field = fields[2].strip()   # "Satcat" column -- 5-digit NORAD-style ID, may be blank ("-")
        if not satcat_field or satcat_field == "-" or not satcat_field.isdigit():
            skipped_blank_id_count += 1
            continue

        try:
            norad = int(satcat_field)
        except ValueError:
            skipped_blank_id_count += 1
            continue

        result[norad] = {
            "owner":  fields[9].strip()  if len(fields) > 9  else "",
            "state":  fields[10].strip() if len(fields) > 10 else "",
            "status": fields[12].strip() if len(fields) > 12 else "",
            "ldate":  fields[7].strip()  if len(fields) > 7  else "",
        }
        parsed_count += 1

    # Sanity-check the parse: if almost every line has a valid-looking
    # NORAD ID in column 2 (so the file structure itself looks right)
    # but the owner/state/status/ldate fields are coming back blank
    # for nearly everyone, that's a strong signal the column layout
    # has shifted even though parsing "succeeded" with no exception --
    # the same kind of silent failure mode as the gp_history malformed-
    # response bug, just one column index off instead of a wrong
    # response format entirely.
    if result:
        sample = list(result.values())[:200]
        all_blank_owner = sum(1 for r in sample if not r["owner"])
        all_blank_state = sum(1 for r in sample if not r["state"])
        if len(sample) >= 50 and all_blank_owner / len(sample) > 0.9 and all_blank_state / len(sample) > 0.9:
            print(
                f"  WARNING: GCAT catalog parsed {parsed_count:,} rows with "
                f"valid NORAD IDs, but owner/state fields are blank for "
                f"{all_blank_owner}/{len(sample)} of a sample check -- this "
                f"usually means GCAT's column layout has changed since this "
                f"parser's column indices (2, 7, 9, 10, 12) were last "
                f"verified. GCAT data will be present but largely empty "
                f"this run. Header line(s) found in the file: "
                f"{header_lines if header_lines else '(none -- file has no comment/header lines)'}",
                flush=True,
            )

    return result


def extract_tle_records(tle_file):
    """
    Lightweight parse of a TLE file into {norad_id: (line1, line2)},
    without constructing EarthSatellite/SGP4 objects. Used for fast
    catalog-to-catalog comparisons (see catalog_diff.py) where only
    the raw TLE text is needed, not a propagator -- avoids the same
    per-record overhead parse_tle_orbital_elements() was already
    written to avoid elsewhere.

    Uses the same blank-line-robust marker scan as load_satellites().
    """

    with open(tle_file, "r") as f:
        raw_lines = [line.rstrip("\n") for line in f.readlines()]

    lines = [line for line in raw_lines if line.strip() != ""]

    records = {}
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if line.startswith("1 ") and i + 1 < n and lines[i + 1].startswith("2 "):
            norad_id = int(line[2:7].strip())
            records[norad_id] = (line, lines[i + 1])
            i += 2
            continue
        i += 1

    return records


# =====================================================
# LOAD SATELLITES
# =====================================================

def load_satellites(tle_file):
    """
    Load all satellites from a TLE file.

    FIX: The original implementation assumed a rigid 3-line stride
    (name, line1, line2) and silently swallowed any parsing
    exception with `except: continue`. If the file ever contained
    a blank line, a missing name line, or any malformed record,
    every satellite *after* that point would be parsed from the
    wrong offsets -- silently, since the bad records were just
    skipped instead of flagged. This version instead scans for the
    TLE line markers ("1 " / "2 ") themselves, so it stays
    synchronized even if blank lines or a missing name line are
    present, and it reports how many records were skipped instead
    of hiding it.
    """

    ts = load.timescale()

    satellites = []
    skipped = 0

    with open(tle_file, "r") as f:
        raw_lines = [line.rstrip("\n") for line in f.readlines()]

    # Drop blank lines up front -- these are the single biggest
    # cause of the old fixed-stride parser desyncing.
    lines = [line for line in raw_lines if line.strip() != ""]

    i = 0
    n = len(lines)

    while i < n:

        line = lines[i]

        is_line1 = line.startswith("1 ")
        next_is_line2 = (
            i + 1 < n and lines[i + 1].startswith("2 ")
        )

        if is_line1 and next_is_line2:

            line1 = line
            line2 = lines[i + 1]

            # The name line, if present, is whatever immediately
            # precedes line1 (as long as it isn't itself a TLE
            # data line).
            name = None
            if i > 0:
                prev = lines[i - 1]
                if not prev.startswith("1 ") and not prev.startswith("2 "):
                    name = prev.strip()

            if not name:
                # Fall back to the catalog number embedded in line1
                # (columns 3-7) so we still get a usable label.
                name = f"UNKNOWN-{line1[2:7].strip()}"

            try:
                sat = EarthSatellite(line1, line2, name, ts)
                satellites.append(sat)
            except Exception:
                skipped += 1

            i += 2
            continue

        i += 1

    print(f"Loaded {len(satellites)} satellites ({skipped} skipped due to parse errors).")

    return satellites, ts


# =====================================================
# ORBIT CLASSIFICATION
# =====================================================

def classify_orbit(sat):
    """
    Classify orbit type: GEO, MEO, LEO, or HEO.

    FIX: The original logic was:
        if 35000 <= altitude <= 37000: return "GEO"
        if altitude > 2000: return "MEO"
        return "LEO"

    Anything with low eccentricity but an altitude *above* 37000 km
    (e.g. graveyard orbits a few hundred km above GEO) fell through
    into the `altitude > 2000` check and was mislabeled "MEO". This
    version explicitly buckets altitudes above the GEO band as HEO
    instead of letting them default into MEO.
    """

    MU = 398600.4418  # km^3/s^2
    EARTH_RADIUS_KM = 6378.137  # equatorial radius (approximation)

    n_rad_sec = sat.model.no_kozai / 60  # rad/min -> rad/sec

    a = (MU / n_rad_sec ** 2) ** (1 / 3)  # semi-major axis, km

    altitude = a - EARTH_RADIUS_KM

    eccentricity = sat.model.ecco

    if eccentricity > 0.5:
        return "HEO"

    if 35000 <= altitude <= 37000:
        return "GEO"

    if altitude > 37000:
        # Above the GEO band but not highly eccentric -- e.g.
        # graveyard / super-synchronous orbits. Previously this
        # fell through and was mislabeled MEO.
        return "HEO"

    if altitude > 2000:
        return "MEO"

    return "LEO"


# =====================================================
# SENSOR FIELD OF REGARD
# =====================================================

def in_sensor_field_of_regard(
    altitude_deg,
    azimuth_deg,
    min_elevation_deg,
    boresight_azimuth_deg,
    azimuth_half_width_deg,
    max_elevation_deg=90.0
):
    """
    Boolean mask: which points fall within the sensor's actual
    field of regard, not just generic full-sky horizon visibility.

    A fixed-facing phased-array radar can't point anywhere above the
    horizon the way a generic "visible if above 0 deg elevation"
    check assumes -- it has both an azimuth sweep limit and,
    sometimes, an elevation ceiling below 90 deg (e.g. PAVE PAWS's
    documented 3-85 deg elevation coverage, vs. AN/FPS-85's
    effectively-90-deg ceiling).

    altitude_deg / azimuth_deg: numpy arrays (or scalars) of
    topocentric altitude/azimuth in degrees.
    boresight_azimuth_deg: center of the antenna's azimuth coverage
    (180 = due south).
    azimuth_half_width_deg: how far the beam can deflect to either
    side of boresight (e.g. 60 -> 120 deg total coverage).
    max_elevation_deg: elevation ceiling (default 90, i.e. no extra
    restriction beyond the natural straight-up cap).
    """

    altitude_deg = np.asarray(altitude_deg)
    azimuth_deg = np.asarray(azimuth_deg)

    elevation_ok = (altitude_deg >= min_elevation_deg) & (altitude_deg <= max_elevation_deg)

    # Angular distance from boresight, correctly handling wraparound
    # at 0/360 degrees (e.g. azimuth 10 vs. boresight 350 is only
    # 20 deg apart, not 340).
    az_diff = np.abs(
        ((azimuth_deg - boresight_azimuth_deg + 180) % 360) - 180
    )
    azimuth_ok = az_diff <= azimuth_half_width_deg

    return elevation_ok & azimuth_ok
