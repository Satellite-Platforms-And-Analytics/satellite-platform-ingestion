"""
Persistent local cache for Space-Track gp_history (TLE history) data.

WHY THIS EXISTS
================
Space-Track's documented API usage policy
(https://www.space-track.org/documentation -> "API Use Guidelines" ->
"Retrieval Strategy" table) lists gp_history as:

    GP_HISTORY   1 / lifetime   Do NOT use this class to retrieve
    current ephemerides; use the GP class. For queries of many
    objects or large date ranges, download TLEs bundled as zip files
    by year from our cloud storage site instead. Once you download
    an object's history, you need to store it on your own servers;
    do not download it again.

Before this module existed, historical_accuracy.py re-queried
gp_history for every satellite on every run that didn't already have
a valid scored result in the previous output file -- and ANY
satellite whose query failed for ANY reason (including, as happened
in practice, the account itself being inactive/suspended) was always
retried on every subsequent run, with no local persistence of
whatever raw TLE data *was* successfully fetched along the way.
Re-querying the same satellites' full multi-year history repeatedly
across runs is exactly the usage pattern the "1/lifetime" policy
prohibits, and is a plausible contributing cause of an account being
flagged or suspended.

This module makes gp_history data genuinely persistent: once a
satellite's history has been successfully fetched for a given date
range, it is stored locally and never re-queried for that range
again. Subsequent runs (even with different lookback windows) only
query Space-Track for the INCREMENTAL portion not already covered by
the local cache, not the whole range over again.

STORAGE FORMAT
===============
A single SQLite database (stdlib, no new dependency) rather than one
file per satellite, since a real catalog run covers tens of thousands
of satellites -- one-file-per-satellite would create significant
filesystem overhead and complicate atomic multi-satellite writes.
SQLite gives us atomic transactions, indexed lookups by NORAD ID, and
a single file to back up/inspect.

Schema:
  tle_elements(norad_id INTEGER, epoch TEXT, altitude_km REAL,
               inclination_deg REAL, eccentricity REAL,
               period_min REAL, PRIMARY KEY (norad_id, epoch))
  coverage(norad_id INTEGER PRIMARY KEY, earliest_epoch TEXT,
           latest_epoch TEXT, last_fetched_at TEXT)

coverage tracks the [earliest_epoch, latest_epoch] range that has
actually been fetched from Space-Track for each satellite, so a
later run asking for a DIFFERENT date range can tell exactly which
portion (if any) is missing and only fetch that slice.
"""

import os
import sqlite3
from datetime import datetime, date, timedelta, timezone
from contextlib import contextmanager


def _utc_now_naive():
    """
    UTC 'now' as a naive datetime, matching the naive-datetime
    convention used throughout this codebase (TLE epochs parsed from
    Space-Track data via _parse_tle_epoch in satellite_utils.py are
    naive). datetime.utcnow() does the same thing but is deprecated
    as of Python 3.12 -- this is the documented replacement pattern.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tle_elements (
    norad_id          INTEGER NOT NULL,
    epoch             TEXT    NOT NULL,
    altitude_km       REAL,
    inclination_deg   REAL,
    eccentricity      REAL,
    period_min        REAL,
    PRIMARY KEY (norad_id, epoch)
);

CREATE INDEX IF NOT EXISTS idx_tle_elements_norad
    ON tle_elements (norad_id);

CREATE TABLE IF NOT EXISTS coverage (
    norad_id          INTEGER PRIMARY KEY,
    earliest_epoch    TEXT NOT NULL,
    latest_epoch      TEXT NOT NULL,
    last_fetched_at   TEXT NOT NULL
);
"""


@contextmanager
def _connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # timeout=30: how long sqlite3 will wait/retry internally before
    # raising "database is locked", for the rare case two threads
    # both want to write at the literal same instant.
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        # WAL (write-ahead logging) mode allows concurrent readers
        # alongside a single writer without blocking each other, which
        # matters here because historical_accuracy.py's Pass 1 fetch
        # runs multiple ThreadPoolExecutor worker threads concurrently,
        # each opening its own connection to this same cache file --
        # the default rollback-journal mode serializes ALL access
        # (readers included) behind any in-progress write, which would
        # turn this cache into a concurrency bottleneck across worker
        # threads. WAL only needs to be set once per database file
        # (it's a persistent property of the file, not the connection)
        # but it's cheap to re-set on every connect and avoids needing
        # separate first-time-setup logic.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        # Performance pragmas for bulk import operations.
        # cache_size=-262144 = 256 MB page cache (negative = kilobytes).
        # synchronous=NORMAL is safe with WAL and much faster than FULL --
        # data is not lost on crash, only on OS/power failure (same risk as
        # FULL for most use cases). temp_store=MEMORY avoids temp disk I/O.
        conn.execute("PRAGMA cache_size=-262144")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _to_date(d):
    """Accept a date or datetime, return a date."""
    if hasattr(d, "date") and not isinstance(d, date):
        return d.date()
    return d


def get_coverage(db_path, norad_ids):
    """
    Return {norad_id: (earliest_date, latest_date)} for every
    requested NORAD ID that has ANY cached coverage at all. IDs with
    no cached data are simply absent from the result -- caller should
    treat that the same as "nothing cached yet for this satellite."
    """
    if not norad_ids:
        return {}

    result = {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" * len(norad_ids))
        rows = conn.execute(
            f"SELECT norad_id, earliest_epoch, latest_epoch FROM coverage "
            f"WHERE norad_id IN ({placeholders})",
            list(norad_ids),
        ).fetchall()
        for norad_id, earliest, latest in rows:
            result[norad_id] = (
                datetime.fromisoformat(earliest).date(),
                datetime.fromisoformat(latest).date(),
            )
    return result


# Module-level flag: track which DB paths have already been auto-healed
# this process invocation. Avoids re-running the two COUNT(*) queries
# on every call to split_cached_vs_needed (which is called multiple
# times per run for different satellite subsets).
_auto_healed_dbs: set = set()


def split_cached_vs_needed(db_path, norad_ids, start_date, end_date):
    """
    Given a requested [start_date, end_date] window and a list of
    NORAD IDs, split them into:

      fully_cached   -- NORAD IDs whose cached coverage already spans
                         the entire requested window.
      needs_fetch    -- {norad_id: (fetch_start, fetch_end)} for IDs
                         that are either entirely uncached, or only
                         partially covered.

    Auto-detects if the coverage table is empty despite tle_elements
    having data (which happens when the bulk import didn't write
    coverage correctly) and triggers a one-time rebuild automatically.
    The check is cached per DB path so it only runs once per process.
    """
    start_date = _to_date(start_date)
    end_date   = _to_date(end_date)

    # Auto-heal: check once per DB path per process invocation
    if db_path not in _auto_healed_dbs and os.path.exists(db_path):
        with _connect(db_path) as conn:
            cov_count  = conn.execute("SELECT COUNT(*) FROM coverage").fetchone()[0]
            elem_count = conn.execute("SELECT COUNT(*) FROM tle_elements").fetchone()[0]
        if cov_count == 0 and elem_count > 0:
            print(
                f"\nTLE history cache has {elem_count:,} element records but "
                f"an empty coverage index.\nRebuilding index automatically "
                f"(one-time operation, may take a minute)...",
                flush=True,
            )
            rebuild_coverage_from_elements(db_path)
            print(flush=True)
        _auto_healed_dbs.add(db_path)

    coverage = get_coverage(db_path, norad_ids)

    fully_cached = []
    needs_fetch  = {}

    # Tolerance for the trailing edge of coverage. Satellites whose
    # latest cached epoch is within this many days of today are treated
    # as "fully covered" for gp_history purposes -- the gap is filled
    # by the daily GP snapshot (class/gp, not gp_history) rather than
    # a new gp_history query. Without this, every satellite imported
    # from bulk zip files (whose last epoch is the zip generation date,
    # not today) would show as needing a fetch on every single run
    # until a live gp_history query updated their latest_epoch to today.
    # 730 days (2 years) covers bulk imports from any point in the past
    # two years, regardless of when the zip files were generated.
    _GAP_TOLERANCE_DAYS = 730
    effective_end = end_date - timedelta(days=_GAP_TOLERANCE_DAYS)

    for norad_id in norad_ids:
        cov = coverage.get(norad_id)
        if cov is None:
            # Never been fetched at all -- genuinely need data
            needs_fetch[norad_id] = (start_date, end_date)
            continue

        cached_earliest, cached_latest = cov

        # A satellite is fully cached if its latest epoch is recent
        # enough (within the tolerance window of today).
        #
        # We deliberately do NOT require cached_earliest <= start_date.
        # A satellite launched in 2020 can never have TLE data before
        # 2020, so requiring it to cover a 10-year lookback starting in
        # 2016 would permanently flag it as needing a fetch -- even
        # though the local cache already has its COMPLETE history from
        # Space-Track's perspective. The confidence scorer uses whatever
        # history IS available; it doesn't need data from before launch.
        if cached_latest >= effective_end:
            fully_cached.append(norad_id)
        else:
            # Latest epoch is too old -- need to extend coverage forward
            # from where we left off to today.
            fetch_start = cached_latest + timedelta(days=1)
            fetch_end   = end_date
            if fetch_start <= fetch_end:
                needs_fetch[norad_id] = (fetch_start, fetch_end)
            else:
                fully_cached.append(norad_id)

    return fully_cached, needs_fetch


def store_elements(db_path, requested_norad_ids, elements_by_norad,
                    fetched_start, fetched_end):
    """
    Persist newly-fetched elements to the local cache and record the
    [fetched_start, fetched_end] range as now covered for each
    satellite, merging with any existing coverage record so the
    stored range only ever grows, never shrinks or resets.

    requested_norad_ids: the FULL list of NORAD IDs that were
    actually queried in this fetch -- IMPORTANT: this must be passed
    explicitly and separately from elements_by_norad.keys(), because
    get_historical_orbital_elements_batch's documented contract is
    that a satellite with zero matching records simply isn't a key
    in its result dict at all (see that function's docstring). If
    coverage were only recorded for elements_by_norad's keys, every
    satellite that genuinely has no TLE history in the requested
    range (common for satellites near a 2-year minimum lookback
    window, or ones with sparse tracking) would NEVER get a coverage
    record, and would be silently re-queried on every single
    subsequent run forever -- precisely the repeated-querying
    pattern this whole cache module exists to eliminate.

    elements_by_norad: {norad_id: [element dicts, ...]} as returned
    by get_historical_orbital_elements_batch -- each element dict has
    "epoch" (a datetime), "altitude_km", "inclination_deg",
    "eccentricity", "period_min". May be missing keys for any
    requested_norad_ids that had zero records -- that's expected and
    handled correctly by iterating requested_norad_ids, not this
    dict's keys.

    Safe to call when some/all requested satellites have zero
    elements -- the coverage record is still written for every one
    of them so the next run doesn't re-query that same range again.
    """
    fetched_start = _to_date(fetched_start)
    fetched_end   = _to_date(fetched_end)
    now_iso = _utc_now_naive().isoformat()

    with _connect(db_path) as conn:
        for norad_id in requested_norad_ids:
            elements = elements_by_norad.get(norad_id, [])

            for el in elements:
                epoch = el["epoch"]
                epoch_iso = epoch.isoformat() if hasattr(epoch, "isoformat") else str(epoch)
                conn.execute(
                    "INSERT OR REPLACE INTO tle_elements "
                    "(norad_id, epoch, altitude_km, inclination_deg, "
                    " eccentricity, period_min) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        norad_id, epoch_iso,
                        el.get("altitude_km"), el.get("inclination_deg"),
                        el.get("eccentricity"), el.get("period_min"),
                    ),
                )

            existing = conn.execute(
                "SELECT earliest_epoch, latest_epoch FROM coverage WHERE norad_id = ?",
                (norad_id,),
            ).fetchone()

            if existing:
                merged_earliest = min(
                    datetime.fromisoformat(existing[0]).date(), fetched_start
                )
                merged_latest = max(
                    datetime.fromisoformat(existing[1]).date(), fetched_end
                )
            else:
                merged_earliest, merged_latest = fetched_start, fetched_end

            conn.execute(
                "INSERT OR REPLACE INTO coverage "
                "(norad_id, earliest_epoch, latest_epoch, last_fetched_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    norad_id, merged_earliest.isoformat(),
                    merged_latest.isoformat(), now_iso,
                ),
            )


def load_cached_elements(db_path, norad_ids):
    """
    Return {norad_id: [element dicts, ...]} for the requested NORAD
    IDs, read entirely from the local cache (no network call). Only
    meaningful for IDs already known to be fully_cached via
    split_cached_vs_needed -- this function doesn't itself check
    coverage, it just reads whatever rows exist.
    """
    if not norad_ids:
        return {}

    result = {nid: [] for nid in norad_ids}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" * len(norad_ids))
        rows = conn.execute(
            f"SELECT norad_id, epoch, altitude_km, inclination_deg, "
            f"eccentricity, period_min FROM tle_elements "
            f"WHERE norad_id IN ({placeholders}) ORDER BY norad_id, epoch",
            list(norad_ids),
        ).fetchall()
        for norad_id, epoch_iso, altitude_km, inclination_deg, eccentricity, period_min in rows:
            result[norad_id].append({
                "epoch": datetime.fromisoformat(epoch_iso),
                "altitude_km": altitude_km,
                "inclination_deg": inclination_deg,
                "eccentricity": eccentricity,
                "period_min": period_min,
            })

    return result


def rebuild_coverage_from_elements(db_path, status_callback=None):
    """
    Rebuild the coverage table from scratch using the actual epoch
    values already in tle_elements.

    Used when the coverage table is empty despite tle_elements having
    data -- which happens when BulkImportSession's __exit__ failed
    to write coverage (e.g. the import was interrupted after elements
    were committed but before the coverage update ran), or when the
    import wrote elements directly without updating coverage.

    This is a one-time repair operation. After it runs, all subsequent
    split_cached_vs_needed() calls will return correct results.
    """
    if not os.path.exists(db_path):
        print("Cache does not exist -- nothing to rebuild.", flush=True)
        return 0

    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-262144")
        conn.executescript(_SCHEMA)  # ensure coverage table exists

        # Count elements to give progress feedback
        total = conn.execute("SELECT COUNT(*) FROM tle_elements").fetchone()[0]
        if total == 0:
            print("tle_elements table is empty -- nothing to rebuild from.", flush=True)
            conn.close()
            return 0

        existing_cov = conn.execute("SELECT COUNT(*) FROM coverage").fetchone()[0]
        print(
            f"Rebuilding coverage table from {total:,} element records "
            f"(existing coverage rows: {existing_cov:,})...",
            flush=True,
        )

        # Aggregate min/max epoch per satellite directly in SQLite --
        # much faster than doing it in Python row-by-row
        now_iso = _utc_now_naive().isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO coverage
                (norad_id, earliest_epoch, latest_epoch, last_fetched_at)
            SELECT
                norad_id,
                MIN(epoch),
                MAX(epoch),
                ?
            FROM tle_elements
            GROUP BY norad_id
        """, (now_iso,))
        conn.commit()

        rebuilt = conn.execute("SELECT COUNT(*) FROM coverage").fetchone()[0]
        conn.execute("PRAGMA synchronous=NORMAL")
        print(
            f"Coverage rebuilt: {rebuilt:,} satellites indexed.",
            flush=True,
        )
        return rebuilt
    finally:
        conn.close()


def cache_stats(db_path):
    """
    Return a short human-readable summary of the cache's current
    size, for printing at the start of a run so the user can see how
    much of their catalog is already covered locally before any
    network calls are made.
    """
    if not os.path.exists(db_path):
        return "Local TLE history cache: not yet created (first run)."

    with _connect(db_path) as conn:
        n_satellites = conn.execute(
            "SELECT COUNT(*) FROM coverage"
        ).fetchone()[0]
        n_records = conn.execute(
            "SELECT COUNT(*) FROM tle_elements"
        ).fetchone()[0]

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    return (
        f"Local TLE history cache: {n_satellites:,} satellites, "
        f"{n_records:,} historical records ({size_mb:.1f} MB) -- "
        f"satellites already covered will NOT be re-queried."
    )


# ── Bulk import fast path ─────────────────────────────────────────────

class BulkImportSession:
    """
    High-speed bulk import context manager that keeps a single SQLite
    connection open for the entire duration of a file import.

    The normal store_elements() function opens and closes a new database
    connection on every call -- necessary for concurrent multi-threaded
    access during live scoring, but a 3x speed penalty during sequential
    bulk import where only one writer ever exists. This class keeps the
    connection open across thousands of batches, using executemany() with
    large batch sizes and synchronous=OFF (safe during bulk import --
    data is committed to disk at the end of each batch regardless).

    Usage:
        with BulkImportSession(db_path) as sess:
            sess.insert_batch(rows)   # rows = [(norad_id, epoch_iso, alt, inc, ecc, period), ...]
            sess.insert_batch(rows)
            ...
        # coverage table is updated automatically on __exit__

    The coverage table is updated once per session (on __exit__) rather
    than on every batch -- for a 22-million-record import this eliminates
    millions of redundant coverage row lookups and updates.
    """

    def __init__(self, db_path, coverage_start, coverage_end):
        self.db_path        = db_path
        self.coverage_start = coverage_start
        self.coverage_end   = coverage_end
        self.conn           = None
        self.norad_ids_seen = set()
        self.total_inserted = 0
        # Track min/max epoch per satellite for coverage update at end
        self._min_epoch = {}
        self._max_epoch = {}

    def __enter__(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=60)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=OFF")   # fastest for bulk; safe with WAL
        self.conn.execute("PRAGMA cache_size=-524288") # 512 MB page cache
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        return self

    def insert_batch(self, rows):
        """
        Insert a batch of TLE element rows.
        rows: list of (norad_id, epoch_iso, alt_km, inc_deg, ecc, period_min)
        """
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO tle_elements "
            "(norad_id, epoch, altitude_km, inclination_deg, eccentricity, period_min) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()

        # Track coverage stats without extra queries
        for norad_id, epoch_iso, *_ in rows:
            self.norad_ids_seen.add(norad_id)
            if norad_id not in self._min_epoch or epoch_iso < self._min_epoch[norad_id]:
                self._min_epoch[norad_id] = epoch_iso
            if norad_id not in self._max_epoch or epoch_iso > self._max_epoch[norad_id]:
                self._max_epoch[norad_id] = epoch_iso
        self.total_inserted += len(rows)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn is None:
            return
        try:
            if exc_type is None and self.norad_ids_seen:
                # Update coverage table once for all satellites seen in this session
                now_iso = _utc_now_naive().isoformat()
                cov_rows = [
                    (
                        norad_id,
                        self._min_epoch.get(norad_id, self.coverage_start.isoformat()),
                        self._max_epoch.get(norad_id, self.coverage_end.isoformat()),
                        now_iso,
                    )
                    for norad_id in self.norad_ids_seen
                ]
                self.conn.executemany(
                    "INSERT OR REPLACE INTO coverage "
                    "(norad_id, earliest_epoch, latest_epoch, last_fetched_at) "
                    "VALUES (?,?,?,?)",
                    cov_rows,
                )
                self.conn.commit()
        finally:
            # Restore safe synchronous mode before closing
            try:
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
            self.conn.close()
            self.conn = None
