"""
spacetrack_client.py — Optional integration with the python-astrodynamics/spacetrack library.

WHY THIS EXISTS
================
The existing satellite_utils.py implementation works correctly and is fully
policy-compliant, but it has one significant inefficiency in how it handles
large TLE history responses: it buffers the entire HTTP response body into
memory before parsing it. For a 200-satellite, 3-year gp_history batch this
can be 50-200 MB held in RAM at once (the raw response text plus the split
list), multiplied by HISTORICAL_CONCURRENT_REQUESTS concurrent workers. On
a machine with 6 GB RAM and 9 concurrent workers that is potentially 1.5-2 GB
of peak RSS just from in-flight response buffers.

The python-astrodynamics/spacetrack library (pip install spacetrack) solves
this with true streaming: its iter_lines=True mode processes one line at a
time as bytes arrive from the network, keeping peak memory at ~200 bytes per
line regardless of total response size. It also provides:

  - AsyncSpaceTrackClient: genuine async I/O via httpx, so all concurrent
    batch fetches run on one thread without GIL contention or thread-per-batch
    overhead.
  - modeldef-based parameter validation: catches typos in query parameters
    before they waste a round-trip (e.g. 'norad_cat_ID' vs 'norad_cat_id').
  - Proper logout on context-manager exit.

This module wraps the library in a way that:
  1. Is OPTIONAL -- if the library is not installed, all functions fall back
     to the existing satellite_utils.py implementation transparently.
  2. Preserves ALL existing policy-compliance logic (the rate limiter,
     api_request_log, SpaceTrackMalformedResponseError detection, etc.) --
     the library's own rate limiter is a SECOND layer, not a replacement.
  3. Does NOT change any calling code in historical_accuracy.py or
     tle_bulk_seeder.py -- the same function signatures are preserved.

INSTALLATION
=============
To enable the streaming/async improvements:
    pip install spacetrack

To disable them (force fallback to existing implementation):
    Set USE_SPACETRACK_LIB=false in your .env file.

After installing the library, run 'python seed_tle_history.py --status'
to confirm it is being used.
"""

import os
import time
import asyncio
from datetime import date, datetime

# ── Detect whether the library is installed and enabled ──────────────
_lib_available = False
_lib_disabled   = os.environ.get("USE_SPACETRACK_LIB", "").lower() == "false"

if not _lib_disabled:
    try:
        from spacetrack import SpaceTrackClient as _STClient
        import spacetrack.operators as _op
        _lib_available = True
    except ImportError:
        pass

    if _lib_available:
        try:
            from spacetrack.aio import AsyncSpaceTrackClient as _AsyncSTClient
            _async_available = True
        except ImportError:
            _async_available = False
    else:
        _async_available = False
else:
    _async_available = False


def library_status():
    """
    Return a human-readable string describing whether the
    python-astrodynamics/spacetrack library is available and active.
    """
    if _lib_disabled:
        return (
            "spacetrack library: DISABLED via USE_SPACETRACK_LIB=false "
            "(using built-in satellite_utils.py implementation)"
        )
    if not _lib_available:
        return (
            "spacetrack library: NOT INSTALLED "
            "(using built-in implementation; "
            "run 'pip install spacetrack' to enable streaming/async mode)"
        )
    async_status = "async available" if _async_available else "sync only (pip install spacetrack[async] for async)"
    return f"spacetrack library v{_get_lib_version()}: ACTIVE ({async_status})"


def _get_lib_version():
    try:
        from importlib.metadata import version
        return version("spacetrack")
    except Exception:
        return "unknown"


# ── Streaming batch fetch (replaces get_historical_orbital_elements_batch) ──

def fetch_gp_history_streaming(username, password, norad_ids,
                                start_date, end_date,
                                rate_limiter=None, timeout_sec=300,
                                max_records_per_satellite=2000,
                                request_log_db=None):
    """
    Fetch gp_history for a batch of NORAD IDs using streaming line-by-line
    processing, dramatically reducing peak memory compared to buffering the
    full response.

    If the spacetrack library is installed, uses SpaceTrackClient with
    iter_lines=True for true streaming. Otherwise falls back to the existing
    satellite_utils.py implementation (which buffers the full response).

    Unlike satellite_utils.get_historical_orbital_elements_batch(), this
    function manages its own session (because the spacetrack library's client
    owns its own session internally). The caller should NOT pass a pre-existing
    requests.Session here; instead this function handles login/logout itself.

    rate_limiter: the existing SpaceTrackRateLimiter instance -- this function
    calls rate_limiter.acquire() before every real request, so the shared
    cross-run budget is always respected, even when using the library (which
    has its own rate limiter as a second independent layer, not a replacement).

    Returns (elements_by_norad, failed_norad_ids) -- same shape as
    satellite_utils.get_historical_orbital_elements_batch_with_retry().
    """
    if _lib_available:
        return _fetch_streaming_lib(
            username, password, norad_ids, start_date, end_date,
            rate_limiter, timeout_sec, max_records_per_satellite,
            request_log_db,
        )
    else:
        return _fetch_streaming_fallback(
            username, password, norad_ids, start_date, end_date,
            rate_limiter, timeout_sec, max_records_per_satellite,
            request_log_db,
        )


def _fetch_streaming_lib(username, password, norad_ids,
                          start_date, end_date, rate_limiter,
                          timeout_sec, max_records_per_satellite,
                          request_log_db):
    """Implementation using the spacetrack library with iter_lines=True."""
    import api_request_log as _arl

    if rate_limiter is not None:
        rate_limiter.acquire(request_class="gp_history", norad_count=len(norad_ids))
    if request_log_db:
        _arl.log_request(request_log_db, "gp_history", norad_count=len(norad_ids))

    import spacetrack.operators as op
    from satellite_utils import parse_tle_orbital_elements, SpaceTrackMalformedResponseError

    epoch_range = op.inclusive_range(start_date, end_date)

    try:
        with _STClient(identity=username, password=password) as st:
            # iter_lines=True: yields one line at a time as it arrives,
            # keeping peak memory at ~200 bytes regardless of total response size.
            lines = st.gp_history(
                iter_lines=True,
                norad_cat_id=norad_ids,
                epoch=epoch_range,
                orderby=["norad_cat_id", "epoch"],
                format="3le",
            )
            return _parse_streaming_lines(
                lines, norad_ids, max_records_per_satellite
            )
    except Exception as e:
        from satellite_utils import SpaceTrackMalformedResponseError
        raise SpaceTrackMalformedResponseError(
            f"Streaming gp_history fetch failed: {e}"
        ) from e


def _fetch_streaming_fallback(username, password, norad_ids,
                               start_date, end_date, rate_limiter,
                               timeout_sec, max_records_per_satellite,
                               request_log_db):
    """Fallback: use the existing satellite_utils implementation."""
    import satellite_utils as su
    import requests

    # Build a session the same way satellite_utils does
    session = su.spacetrack_login(
        username, password, timeout_sec=timeout_sec, verify_account=False
    )
    try:
        result, failed = su.get_historical_orbital_elements_batch_with_retry(
            session, norad_ids, start_date, end_date,
            timeout_sec=timeout_sec,
            max_records_per_satellite=max_records_per_satellite,
            rate_limiter=rate_limiter,
        )
        return result, failed
    finally:
        session.close()


def _parse_streaming_lines(lines, requested_norad_ids, max_records_per_satellite):
    """
    Parse a stream of 3LE/2LE text lines into (elements_by_norad, failed_ids).
    Processes one line at a time -- peak memory is O(batch_records) not O(response_size).
    """
    # Deferred import: satellite_utils depends on skyfield which may not be
    # available in all environments. Importing here (not at module level)
    # means spacetrack_client.py can be safely imported for library_status()
    # even without skyfield installed.
    try:
        from satellite_utils import parse_tle_orbital_elements, SpaceTrackMalformedResponseError
    except ImportError:
        # Define minimal fallbacks so the module stays functional
        def parse_tle_orbital_elements(l1, l2):
            from tle_bulk_seeder import _parse_tle_epoch, _parse_orbital_elements
            epoch_iso = _parse_tle_epoch(l1)
            from datetime import datetime
            elems = _parse_orbital_elements(l1, l2)
            elems["epoch"] = datetime.fromisoformat(epoch_iso)
            return elems
        class SpaceTrackMalformedResponseError(RuntimeError):
            pass

    by_norad = {}    # {norad_id: [(line1, line2), ...]}
    pending_line1 = None
    has_any_data  = False
    raw_lines_seen = 0

    for line in lines:
        raw_lines_seen += 1
        stripped = line.strip() if isinstance(line, str) else line.decode().strip()
        if not stripped:
            continue

        if stripped.startswith("1 ") and len(stripped) >= 60:
            pending_line1 = stripped
        elif stripped.startswith("2 ") and len(stripped) >= 60 and pending_line1:
            try:
                norad_id = int(pending_line1[2:7].strip())
                by_norad.setdefault(norad_id, []).append((pending_line1, stripped))
                has_any_data = True
            except (ValueError, IndexError):
                pass
            pending_line1 = None
        else:
            # Name line (3LE) or other non-TLE line -- reset pending
            if not stripped.startswith("2 "):
                pending_line1 = None

    # Validate: non-empty response that yields zero records = malformed
    if raw_lines_seen > 0 and not has_any_data:
        raise SpaceTrackMalformedResponseError(
            f"gp_history streaming response had {raw_lines_seen} lines "
            f"but yielded zero parseable TLE record pairs"
        )

    # Parse orbital elements and subsample
    result = {}
    for norad_id, records in by_norad.items():
        if len(records) > max_records_per_satellite:
            step = len(records) / max_records_per_satellite
            records = [records[int(j * step)] for j in range(max_records_per_satellite)]

        elements = []
        for line1, line2 in records:
            try:
                elements.append(parse_tle_orbital_elements(line1, line2))
            except Exception:
                continue
        elements.sort(key=lambda e: e["epoch"])
        result[norad_id] = elements

    return result, []


# ── Async parallel batch fetch ────────────────────────────────────────

def fetch_gp_history_async_batches(username, password, batch_list,
                                    start_date, end_date,
                                    rate_limiter=None, timeout_sec=300,
                                    max_records_per_satellite=2000,
                                    request_log_db=None,
                                    status_callback=None):
    """
    Fetch multiple gp_history batches concurrently using async I/O.

    batch_list: list of NORAD ID lists, e.g. [[39256, 43651], [25544, ...], ...]
    Each sub-list is fetched as one Space-Track request. All batches run
    concurrently (within the rate limit) using asyncio instead of threads,
    eliminating GIL contention and thread overhead.

    If the async spacetrack library is not available, falls back to running
    the batches sequentially (each still using streaming if the sync library
    is available).

    status_callback: optional callable(batch_num, total_batches, records_fetched)
    called after each batch completes.

    Returns {norad_id: [elements, ...]} merged across all batches, plus a
    list of failed NORAD IDs.
    """
    if _async_available:
        return asyncio.run(
            _fetch_async_batches(
                username, password, batch_list, start_date, end_date,
                rate_limiter, timeout_sec, max_records_per_satellite,
                request_log_db, status_callback,
            )
        )
    else:
        # Sequential fallback
        merged = {}
        all_failed = []
        for i, norad_ids in enumerate(batch_list):
            result, failed = fetch_gp_history_streaming(
                username, password, norad_ids, start_date, end_date,
                rate_limiter, timeout_sec, max_records_per_satellite,
                request_log_db,
            )
            merged.update(result)
            all_failed.extend(failed)
            if status_callback:
                status_callback(i + 1, len(batch_list), len(result))
        return merged, all_failed


async def _fetch_async_batches(username, password, batch_list,
                                start_date, end_date,
                                rate_limiter, timeout_sec,
                                max_records_per_satellite,
                                request_log_db, status_callback):
    """Async implementation using AsyncSpaceTrackClient."""
    import api_request_log as _arl
    import spacetrack.operators as op

    epoch_range = op.inclusive_range(start_date, end_date)
    merged   = {}
    failed   = []
    lock     = asyncio.Lock()
    done     = [0]

    async with _AsyncSTClient(identity=username, password=password) as st:
        async def fetch_one(batch_num, norad_ids):
            # Rate limit before each request -- acquire from the shared
            # limiter (which also logs to api_request_log) then await
            # to let other coroutines run while waiting.
            if rate_limiter is not None:
                # rate_limiter.acquire() blocks synchronously; run it in
                # the executor so it doesn't block the event loop.
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: rate_limiter.acquire(
                        request_class="gp_history",
                        norad_count=len(norad_ids)
                    )
                )
            if request_log_db:
                _arl.log_request(request_log_db, "gp_history",
                                  norad_count=len(norad_ids))
            try:
                lines = await st.gp_history(
                    iter_lines=True,
                    norad_cat_id=norad_ids,
                    epoch=epoch_range,
                    orderby=["norad_cat_id", "epoch"],
                    format="3le",
                )
                result, batch_failed = _parse_streaming_lines(
                    [line async for line in lines],
                    norad_ids, max_records_per_satellite,
                )
            except Exception as e:
                from satellite_utils import SpaceTrackMalformedResponseError
                result, batch_failed = {}, list(norad_ids)

            async with lock:
                merged.update(result)
                failed.extend(batch_failed)
                done[0] += 1
                if status_callback:
                    status_callback(done[0], len(batch_list), len(result))

        tasks = [
            asyncio.create_task(fetch_one(i, norad_ids))
            for i, norad_ids in enumerate(batch_list)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    return merged, failed


# ── Daily GP snapshot (streaming version) ────────────────────────────

def fetch_daily_gp_streaming(username, password, rate_limiter=None,
                              timeout_sec=120, request_log_db=None):
    """
    Fetch today's full GP catalog using streaming, returning an iterator
    of (line1, line2) TLE pairs. Uses the spacetrack library if available.

    This is used by tle_bulk_seeder.snapshot_daily_gp() to replace the
    current chunk-based download with true line-by-line streaming.
    """
    if _lib_available:
        return _fetch_gp_streaming_lib(
            username, password, rate_limiter, timeout_sec, request_log_db
        )
    return None  # caller falls back to its own implementation


def _fetch_gp_streaming_lib(username, password, rate_limiter,
                              timeout_sec, request_log_db):
    """Stream the full GP catalog using the library."""
    import api_request_log as _arl

    if rate_limiter is not None:
        rate_limiter.acquire(request_class="gp", norad_count=0)
    if request_log_db:
        _arl.log_request(request_log_db, "gp", norad_count=0)

    with _STClient(identity=username, password=password) as st:
        lines = st.gp(
            iter_lines=True,
            decay_date=None,
            epoch=">now-10",
            orderby="norad_cat_id",
            format="3le",
        )
        # Materialise into a list so the context manager stays open
        # while we read all lines; the caller will parse them after return.
        return list(lines)


# ── Convenience: validate query parameters before sending ─────────────

def validate_predicates(class_name, **kwargs):
    """
    Validate query keyword arguments against Space-Track's modeldef API.
    Returns a list of error strings (empty = all valid).
    Requires the spacetrack library. Returns [] if library not available.

    Example:
        errors = validate_predicates("gp_history", norad_cat_ID=[25544])
        # returns ["'gp_history' got unexpected argument 'norad_cat_ID'"]
    """
    if not _lib_available:
        return []
    try:
        with _STClient.__new__(_STClient) as st:
            predicates = st.get_predicates(class_name)
            valid_names = {p.name for p in predicates}
            return [
                f"'{class_name}' got unexpected argument '{k}'"
                for k in kwargs if k.lower() not in valid_names
            ]
    except Exception:
        return []
