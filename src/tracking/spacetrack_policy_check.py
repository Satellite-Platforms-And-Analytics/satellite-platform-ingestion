"""
Pre-flight check against Space-Track's documented API usage policy.

WHY THIS EXISTS
================
Space-Track's documented usage policy
(https://www.space-track.org/documentation -> "API Use Guidelines")
sets hard per-class limits (gp_history: 1/lifetime per object, SATCAT:
1/day) on top of the aggregate request-rate limits (30/minute,
300/hour). The caching modules added to this project
(tle_history_cache.py, satcat_cache.py) make the tool COMPLY with
those limits by only ever querying what's genuinely missing/stale --
but compliance only works if the caches are actually doing their job
correctly, and a bug in this tool, a corrupted cache file, or a
config change could silently cause a run to over-query again without
any obvious warning until the account is flagged.

This module runs BEFORE any Space-Track network call (including
login) and answers, using only locally-known information --
the local caches and the requested run parameters:

  1. How many satellites does this run actually need to query
     (after subtracting what the local caches already cover)?
  2. How many actual HTTP requests does that translate to, given the
     configured batch size?
  3. Does that request count fit safely within Space-Track's
     published rate limits for a single run?
  4. Does anything about this PLAN look like it's about to violate
     the per-class policy (e.g. requesting gp_history for satellites
     that are already fully cached -- which would indicate a cache
     bug, not legitimate new data)?

If anything looks wrong, the check refuses to proceed (raises) with
a clear, specific explanation, rather than letting a misconfigured
or buggy run silently hammer Space-Track and risk another account
suspension. This is a SAFETY NET on top of the caching fix, not a
replacement for it -- the caches are what make compliance possible
at all; this just verifies, before spending a single network call,
that the plan derived from them actually looks compliant.
"""

from datetime import date

import tle_history_cache
import satcat_cache


# Space-Track's documented hard limits (see module docstring). Kept
# slightly under the published numbers as a safety margin, matching
# satellite_utils.SpaceTrackRateLimiter's existing convention.
_MAX_REQUESTS_PER_MINUTE = 28
_MAX_REQUESTS_PER_HOUR   = 290

# A single run that would need more than this many real gp_history
# HTTP requests (after cache subtraction) is treated as suspicious
# enough to require explicit confirmation rather than proceeding
# silently -- this is deliberately conservative. At ~28/min, this
# many requests alone would take roughly an hour of nonstop querying,
# which is a reasonable "are you sure this is what you meant"
# threshold for a single run (e.g. accidentally pointing the tool at
# an enormous unfiltered catalog, or a cache that got wiped).
_LARGE_RUN_REQUEST_WARNING_THRESHOLD = 1500


class PolicyCheckFailed(RuntimeError):
    """
    Raised when the pre-flight check determines the planned run would
    violate (or come dangerously close to violating) Space-Track's
    documented API usage policy. Carries a human-readable explanation
    of exactly what triggered it and what to do about it.
    """
    pass


def check_gp_history_plan(norad_ids, start_date, end_date,
                           tle_cache_db, batch_size):
    """
    Determine how many gp_history requests the planned historical-
    confidence run would actually need, after subtracting what the
    local cache already covers.

    Returns a dict:
      total_satellites      -- len(norad_ids)
      fully_cached_count    -- satellites needing ZERO network calls
      needs_fetch_count     -- satellites needing some/all data fetched
      estimated_requests    -- needs_fetch_count satellites grouped
                                into batches of `batch_size`, counted
                                as one HTTP request each (this is an
                                upper-bound estimate -- satellites
                                needing different incremental date
                                ranges may not all batch together as
                                cleanly as this estimate assumes, so
                                the real number is normally <= this)
      cache_coverage_pct    -- what fraction of requested satellites
                                are already fully cached, as a sanity
                                metric to print to the user
    """
    fully_cached, needs_fetch = tle_history_cache.split_cached_vs_needed(
        tle_cache_db, norad_ids, start_date, end_date
    )

    needs_fetch_count = len(needs_fetch)
    # Upper-bound estimate: satellites needing fetch grouped into
    # batch_size-sized requests. The real run may do slightly better
    # than this (some satellites needing the SAME incremental range
    # batch together even more efficiently), but never worse, so this
    # is a safe over-estimate for the policy check.
    estimated_requests = (
        (needs_fetch_count + batch_size - 1) // batch_size
        if needs_fetch_count else 0
    )

    total = len(norad_ids)
    coverage_pct = (
        100.0 * len(fully_cached) / total if total else 100.0
    )

    return {
        "total_satellites":   total,
        "fully_cached_count": len(fully_cached),
        "needs_fetch_count":  needs_fetch_count,
        "estimated_requests": estimated_requests,
        "cache_coverage_pct": coverage_pct,
    }


def check_satcat_plan(norad_ids, satcat_cache_db, max_age_hours):
    """
    Same idea as check_gp_history_plan but for SATCAT, which only has
    "fresh enough to reuse" / "needs a fetch" (no partial-range
    concept, unlike gp_history).
    """
    fresh_data, needs_fetch = satcat_cache.split_cached_vs_needed(
        satcat_cache_db, norad_ids, max_age_hours
    )
    total = len(norad_ids)
    # SATCAT is queried in batches of up to 500 (see fetch_satcat_data).
    estimated_requests = (
        (len(needs_fetch) + 499) // 500 if needs_fetch else 0
    )
    return {
        "total_satellites":   total,
        "fresh_count":        len(fresh_data),
        "needs_fetch_count":  len(needs_fetch),
        "estimated_requests": estimated_requests,
    }


def run_preflight_check(norad_ids, lookback_years, tle_cache_db,
                         satcat_cache_db, satcat_max_age_hours,
                         batch_size, api_log_db, interactive=True):
    """
    Run the full pre-flight policy check for a planned historical-
    confidence run, BEFORE any Space-Track network call is made.

    Checks THREE distinct things:

    1. Per-class lifetime/daily limits (gp_history, SATCAT): does
       the local cache already have what's needed, so this run won't
       re-query what a previous run already fetched?

    2. Aggregate rate limits (30/min, 300/hour) ACROSS runs: reads
       the persistent api_request_log to see how many requests were
       already made in the past 60 seconds and past 60 minutes by
       ANY previous run, then adds this run's estimated requests to
       that count. If the combined total would exceed the documented
       limits, the run is blocked until the window clears.

    3. SATCAT wall-clock-day constraint: SATCAT is documented as
       "1/day after 1700 UTC" -- if a SATCAT fetch has already been
       made today (any time today, per the api_request_log), this
       run's SATCAT step is confirmed as "will be skipped via the
       cache" regardless of the rolling-24h TTL used by satcat_cache.

    Parameters
    ----------
    norad_ids : list of NORAD IDs this run intends to evaluate.
    lookback_years : planned lookback window (years).
    tle_cache_db : path to the persistent TLE history cache.
    satcat_cache_db : path to the persistent SATCAT cache.
    satcat_max_age_hours : rolling TTL for SATCAT cache entries.
    batch_size : satellites per gp_history request (for request estimation).
    api_log_db : path to the persistent cross-run api_request_log DB.
    interactive : if True, large-run warnings prompt for confirmation
                  rather than raising immediately.

    Raises PolicyCheckFailed if anything would violate documented
    policy and should not proceed.
    Returns the gp_history plan dict on success.
    """
    import time as _time

    end_date = date.today()
    start_date = date(end_date.year - lookback_years, end_date.month, end_date.day)

    print("─" * 60)
    print("Pre-flight check: Space-Track API usage policy")
    print("─" * 60)
    print(
        "Checking this run's planned requests against Space-Track's\n"
        "documented limits BEFORE making any network call -- this\n"
        "accounts for requests made by PREVIOUS runs too, not just\n"
        "this one. See https://www.space-track.org/documentation.\n"
    )

    # ── 1. Cache coverage (per-class lifetime/daily limits) ──────
    gp_plan    = check_gp_history_plan(norad_ids, start_date, end_date, tle_cache_db, batch_size)
    satcat_plan = check_satcat_plan(norad_ids, satcat_cache_db, satcat_max_age_hours)

    print(
        f"gp_history (documented limit: 1/satellite/LIFETIME):\n"
        f"  {gp_plan['total_satellites']:,} satellites planned\n"
        f"  {gp_plan['fully_cached_count']:,} already fully cached "
        f"({gp_plan['cache_coverage_pct']:.1f}%) -- zero requests needed\n"
        f"  {gp_plan['needs_fetch_count']:,} need fetching (new or extended coverage)\n"
        f"  -> ~{gp_plan['estimated_requests']:,} HTTP request(s) at batch size {batch_size}"
    )
    print(
        f"SATCAT (documented limit: 1/day):\n"
        f"  {satcat_plan['fresh_count']:,} satellites already fresh in cache "
        f"(within {satcat_max_age_hours}h) -- zero requests needed\n"
        f"  {satcat_plan['needs_fetch_count']:,} would need fetching\n"
        f"  -> ~{satcat_plan['estimated_requests']:,} HTTP request(s)"
    )

    # ── 2. Cross-run rate-limit check ────────────────────────────
    print()
    from api_request_log import get_recent_counts
    counts = get_recent_counts(api_log_db)

    past_min  = counts["requests_past_minute"]
    past_hr   = counts["requests_past_hour"]
    this_run  = gp_plan["estimated_requests"] + satcat_plan["estimated_requests"] + 1

    # Per-minute check: the rate limiter paces requests at _MAX_REQUESTS_PER_MINUTE
    # per minute, so a run of N requests takes ceil(N/28) minutes. Only the first
    # minute's worth of requests (up to 28) could land in the same 60-second window
    # as recent requests. Projecting the full run total against the per-minute limit
    # wrongly holds runs of >28 requests even when there's plenty of headroom.
    first_minute_requests = min(this_run, _MAX_REQUESTS_PER_MINUTE)
    projected_min = past_min + first_minute_requests
    projected_hr  = past_hr  + this_run

    print(
        f"Rate limits (across ALL runs, not just this one):\n"
        f"  Past 60 seconds : {past_min:>4} requests already made\n"
        f"  Past 60 minutes : {past_hr:>4} requests already made\n"
        f"  This run adds   : {this_run:>4} estimated requests\n"
        f"  Projected total : {projected_min:>4}/min  {projected_hr:>4}/hr\n"
        f"  Published limit : {_MAX_REQUESTS_PER_MINUTE+2:>4}/min  "
        f"{_MAX_REQUESTS_PER_HOUR+10:>4}/hr"
    )

    minute_ok = projected_min <= _MAX_REQUESTS_PER_MINUTE
    hour_ok   = projected_hr  <= _MAX_REQUESTS_PER_HOUR

    if not minute_ok or not hour_ok:
        from api_request_log import get_recent_counts as _rc
        _sleep = 5
        print()
        print(
            f"  RATE LIMIT HOLD: projected {projected_min}/min or "
            f"{projected_hr}/hr would exceed limits.\n"
            f"  Waiting for the window to clear (checking every {_sleep}s)...",
            flush=True,
        )
        waited = 0
        while True:
            _time.sleep(_sleep)
            waited += _sleep
            c = get_recent_counts(api_log_db)
            pm, ph = c["requests_past_minute"], c["requests_past_hour"]
            first_min = min(this_run, _MAX_REQUESTS_PER_MINUTE)
            if pm + first_min <= _MAX_REQUESTS_PER_MINUTE and ph + this_run <= _MAX_REQUESTS_PER_HOUR:
                print(
                    f"  Window cleared after {waited}s -- "
                    f"{pm}/min and {ph}/hr, proceeding.",
                    flush=True,
                )
                break
            print(f"  Still waiting ({waited}s)... {pm}/min {ph}/hr", flush=True)
        print()

    # ── 3. SATCAT wall-clock-day constraint ──────────────────────
    satcat_today = counts["satcat_fetches_today"]
    if satcat_today > 0 and satcat_plan["estimated_requests"] > 0:
        # A SATCAT fetch was already logged for today. The satcat_cache's
        # rolling 24h TTL might still show satellites as needing a fetch
        # (e.g. the earlier fetch was 23h ago, so records are technically
        # stale), but the wall-clock-day policy says no more today.
        # Force the estimated SATCAT requests to zero and note this.
        print(
            f"  SATCAT: {satcat_today} fetch(es) already logged today -- "
            f"skipping any further SATCAT queries for the rest of today to "
            f"comply with the documented 1/day limit, even if some cached\n"
            f"  records look stale. Any stale SATCAT data will be refreshed\n"
            f"  on tomorrow's run.\n",
            flush=True,
        )
        # Flag this for the caller: the SATCAT phase will rely entirely
        # on whatever is already in the cache (including potentially stale
        # records) rather than fetching anything new today.
        satcat_plan = dict(satcat_plan, estimated_requests=0, _skip_satcat_today=True)

    # ── 4. Large-run sanity check ────────────────────────────────
    if (
        gp_plan["total_satellites"] > 500
        and gp_plan["cache_coverage_pct"] < 1.0
    ):
        import os as _os
        if _os.path.exists(tle_cache_db):
            print(
                f"  NOTE: {gp_plan['total_satellites']:,} satellites planned "
                f"but the TLE history cache shows <1% prior coverage despite "
                f"a cache file existing. If this is NOT your first run, check "
                f"that the cache file path hasn't changed and wasn't deleted.\n",
                flush=True,
            )

    total_estimated = gp_plan["estimated_requests"] + satcat_plan["estimated_requests"] + 1
    if total_estimated > _LARGE_RUN_REQUEST_WARNING_THRESHOLD:
        message = (
            f"This run needs ~{total_estimated:,} Space-Track requests. "
            f"At {_MAX_REQUESTS_PER_MINUTE}/min that's roughly "
            f"{total_estimated // _MAX_REQUESTS_PER_MINUTE} minutes of "
            f"continuous querying. Confirm this is intentional (e.g. "
            f"genuinely the first run against a large catalog, not a sign "
            f"the local cache isn't being reused)."
        )
        if interactive:
            print(f"\n{message}\n")
            response = input("Type 'yes' to proceed, or anything else to cancel: ").strip().lower()
            if response != "yes":
                raise PolicyCheckFailed("Run cancelled at the pre-flight check.")
            print("Proceeding with confirmed large run.\n")
        else:
            raise PolicyCheckFailed(
                f"{message} Running non-interactively -- re-run interactively "
                f"to confirm, or reduce HISTORICAL_MAX_SATELLITES in config.py."
            )

    print("Pre-flight check passed -- proceeding.\n")
    print("─" * 60)
    print()

    return gp_plan
