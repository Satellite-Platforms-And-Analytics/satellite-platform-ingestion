"""
Persistent cross-run request log for Space-Track API calls.

WHY THIS EXISTS
================
Space-Track's rate limits (30/minute, 300/hour) and per-class
constraints (SATCAT: 1/day) apply across ALL usage of your account,
not just within a single program invocation. A SpaceTrackRateLimiter
inside a running process correctly paces requests within that process,
but it resets completely on the next run. Two back-to-back runs 30
seconds apart could each look "fine" to their own internal limiters
while together violating the 300/hour ceiling.

Similarly, the SATCAT 24h TTL cache prevents re-querying the same
satellite's metadata within 24 rolling hours, but it can't enforce
the wall-clock "once per calendar day" intent -- two runs 10 minutes
apart across a calendar-day boundary (11:58 PM and 12:02 AM) would
each see stale SATCAT data and each trigger a new fetch, producing
two SATCAT queries within 10 minutes.

This module solves both gaps by logging every real Space-Track HTTP
request to a persistent SQLite file, timestamped to the second.
The pre-flight check reads this log to compute:

  - requests_in_past_minute  -- must stay under 30
  - requests_in_past_hour    -- must stay under 300
  - satcat_fetches_today     -- must stay at 0 (already fetched today
                                 = don't fetch again this calendar day)

The log is append-only and automatically purged of entries older than
25 hours (enough for a full hourly window with margin) so it stays
small regardless of how long the tool has been in use.

IMPORTANT: this log tracks requests at the point they're LOGGED, not
at the point they're planned. It's written to by satellite_utils.py's
rate limiter at the moment each real HTTP request is made. The pre-
flight check reads it before each run to project "if I make N more
requests, will the total cross a limit?" -- it's the combination of
what's already in the log PLUS what the current run is about to add
that the check validates, not either one in isolation.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    class       TEXT NOT NULL,
    norad_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_requests_timestamp
    ON requests (timestamp);
"""

# Classes that count toward the aggregate per-minute/hour limits.
CLASS_GP_HISTORY  = "gp_history"
CLASS_SATCAT      = "satcat"
CLASS_LOGIN       = "login"
CLASS_OTHER       = "other"


def _utc_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def _connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_request(db_path, request_class, norad_count=0):
    """
    Record that one HTTP request was just made to Space-Track.

    request_class: one of the CLASS_* constants above.
    norad_count: how many NORAD IDs were included in this request
                 (informational only, not used for rate-limit math
                 since Space-Track counts per-request not per-ID).

    Call this immediately AFTER a real request succeeds, not before,
    so the log reflects actual requests made, not just planned ones.
    Failing/retried requests should only be logged if they actually
    consumed a slot on Space-Track's side (i.e., got a real response,
    even an error one, rather than timing out before connecting).
    """
    now_iso = _utc_now_naive().isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO requests (timestamp, class, norad_count) VALUES (?, ?, ?)",
            (now_iso, request_class, norad_count),
        )
        # Prune entries older than 25 hours while we have the connection
        # open anyway -- keeps the file small without a separate job.
        cutoff = (_utc_now_naive() - timedelta(hours=25)).isoformat()
        conn.execute("DELETE FROM requests WHERE timestamp < ?", (cutoff,))


def get_recent_counts(db_path):
    """
    Return a dict of request counts over the windows that matter for
    Space-Track's documented rate limits:

      requests_past_minute  -- last 60 seconds (limit: 30)
      requests_past_hour    -- last 3600 seconds (limit: 300)
      satcat_fetches_today  -- SATCAT requests since midnight UTC today
                               (policy says "once per day after 1700 UTC")
      last_satcat_time      -- ISO timestamp of most recent SATCAT fetch,
                               or None if no SATCAT fetches logged at all
    """
    if not os.path.exists(db_path):
        return {
            "requests_past_minute": 0,
            "requests_past_hour":   0,
            "satcat_fetches_today": 0,
            "last_satcat_time":     None,
        }

    now = _utc_now_naive()
    one_minute_ago  = (now - timedelta(seconds=60)).isoformat()
    one_hour_ago    = (now - timedelta(seconds=3600)).isoformat()
    midnight_today  = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    with _connect(db_path) as conn:
        past_minute = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE timestamp >= ?",
            (one_minute_ago,),
        ).fetchone()[0]

        past_hour = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE timestamp >= ?",
            (one_hour_ago,),
        ).fetchone()[0]

        satcat_today = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE class = ? AND timestamp >= ?",
            (CLASS_SATCAT, midnight_today),
        ).fetchone()[0]

        last_satcat_row = conn.execute(
            "SELECT timestamp FROM requests WHERE class = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (CLASS_SATCAT,),
        ).fetchone()
        last_satcat_time = last_satcat_row[0] if last_satcat_row else None

    return {
        "requests_past_minute": past_minute,
        "requests_past_hour":   past_hour,
        "satcat_fetches_today": satcat_today,
        "last_satcat_time":     last_satcat_time,
    }


def headroom(db_path):
    """
    Return how many more requests can be made right now without
    violating either rate limit -- the MINIMUM of:
      - (30 - requests_past_minute)
      - (300 - requests_past_hour)

    Returns a tuple: (minute_headroom, hour_headroom, effective_headroom)
    where effective_headroom = min(minute, hour). Zero or negative
    means no requests should be made until a window clears.
    """
    counts = get_recent_counts(db_path)
    minute_hr = max(0, 30 - counts["requests_past_minute"])
    hour_hr   = max(0, 300 - counts["requests_past_hour"])
    return minute_hr, hour_hr, min(minute_hr, hour_hr)
