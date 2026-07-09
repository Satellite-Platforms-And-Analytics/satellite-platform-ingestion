"""
Bulk TLE history seeder and daily snapshot manager.

THREE COMPLEMENTARY STRATEGIES
================================

1. ONE-TIME BULK SEED FROM CLOUD STORAGE  (seed_from_publicfiles)
   ---------------------------------------------------------------
   Downloads the pre-packaged yearly zip files that Space-Track hosts
   on their cloud storage site (Sync.com), accessed via the publicfiles
   API endpoint. The official documentation for gp_history says:
     "For queries of many objects or large date ranges, download TLEs
      bundled as zip files by year from our cloud storage site instead."
   This is the FILES PANEL policy class (1/lifetime per file), meaning
   download each year's zip ONCE and never again. Each year is a
   single large zip containing every TLE epoch published that year for
   every tracked object. This is the fastest way to seed years of
   history in one operation without violating the gp_history 1/lifetime
   per-satellite policy, since the per-year zips are a separate
   class (Files Panel).

2. INCREMENTAL DELTA SYNC  (sync_incremental_delta)
   -------------------------------------------------
   After the initial bulk seed, uses the FILE predicate documented in
   Space-Track's How-To page ("How to download just the changes since
   your last download") to fetch only TLE records published after the
   last known file number. One gp_history request per sync, pulling
   only the new/changed records since the previous sync. The bookmark
   (highest FILE number seen) is stored in the local cache database
   and automatically updated after each successful sync.

3. DAILY GP SNAPSHOT  (snapshot_daily_gp)
   ----------------------------------------
   One class/gp request per day for the full current catalog -- NOT
   gp_history. Under the GP: 1/hour policy class, completely separate.
   Captures each day's best-fit TLE for every on-orbit object, adds
   it to the SQLite history cache, and writes a daily zip archive.

RECOMMENDED WORKFLOW
=====================
  First run: seed_from_publicfiles()  -- download yearly zips once
  Ongoing:   sync_incremental_delta() -- pull only what's changed
  Daily:     snapshot_daily_gp()      -- capture today's GP state

MEMORY EFFICIENCY
==================
All responses are streamed and processed in fixed-size chunks.
Peak memory is bounded by the streaming buffer regardless of
response or zip file size.
"""

import os
import io
import time
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

SPACETRACK_BASE = "https://www.space-track.org"
PUBLICFILES_DIRS  = f"{SPACETRACK_BASE}/publicfiles/query/class/dirs"
PUBLICFILES_FILES  = f"{SPACETRACK_BASE}/publicfiles/query/class/files"
PUBLICFILES_INFO   = f"{SPACETRACK_BASE}/publicfiles/query/class/loadpublicdata"
PUBLICFILES_DL     = f"{SPACETRACK_BASE}/publicfiles/query/class/download"
BASICSPACEDATA     = f"{SPACETRACK_BASE}/basicspacedata/query"


# ──────────────────────────────────────────────────────────────────────
# Archive directory helpers
# ──────────────────────────────────────────────────────────────────────

def import_from_local_folder(folder_path, cache_db_path, tle_data_dir,
                              start_year=None, end_year=None,
                              status_callback=None):
    """
    Import TLE data from locally-downloaded Space-Track publicfiles zip
    files into the SQLite history cache and local archive.

    This is the function to call when you've already downloaded the
    yearly zip files from Space-Track's cloud storage (Sync.com) and
    stored them locally -- no network connection or credentials needed.

    Handles both naming patterns present in the Space-Track bulk files:
      tle{YYYY}.txt.zip       -- single file per year (2005-2025)
      tle{YYYY}_{N}of{M}.txt.zip -- multi-part year (e.g. 2004 in 8 parts)

    Each zip contains a single .txt file in 2LE format (standard
    two-line element sets, no name/title line). Multi-part files for the
    same year are automatically combined and treated as one year.

    start_year / end_year: only import years within this range. Default
    is all available years. For a 3-year confidence scoring lookback,
    set start_year = current_year - 3. For a 5-year lookback, use
    start_year = current_year - 5. Importing extra years costs disk
    space and time but doesn't hurt confidence scoring accuracy.

    Files are processed in chronological order, oldest first, so the
    coverage records in the SQLite cache accurately reflect what's been
    loaded when checked by split_cached_vs_needed().

    Already-imported data is safely overwritten (INSERT OR REPLACE) --
    re-importing the same zip a second time is safe and idempotent,
    just redundant work.

    Returns a summary dict with total records and satellites ingested.
    """
    import re

    if not os.path.isdir(folder_path):
        raise ValueError(f"Folder not found: {folder_path}")

    # Parse all zip filenames in the folder
    pattern_single = re.compile(r"^tle(\d{4})\.txt\.zip$", re.IGNORECASE)
    pattern_multi  = re.compile(r"^tle(\d{4})_(\d+)of(\d+)\.txt\.zip$", re.IGNORECASE)

    # Group files by year: {year: [(part_num, total_parts, filepath), ...]}
    by_year = {}
    for fname in os.listdir(folder_path):
        fpath = os.path.join(folder_path, fname)
        if not os.path.isfile(fpath) or not fname.lower().endswith(".zip"):
            continue

        m = pattern_multi.match(fname)
        if m:
            year, part, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
            by_year.setdefault(year, []).append((part, total, fpath))
            continue

        m = pattern_single.match(fname)
        if m:
            year = int(m.group(1))
            by_year.setdefault(year, []).append((1, 1, fpath))
            continue

    if not by_year:
        print(f"No tle*.txt.zip files found in {folder_path}", flush=True)
        return {"years_processed": 0, "total_records": 0, "total_sats": 0}

    # Apply year range filter
    years_to_process = sorted(by_year.keys())
    if start_year:
        years_to_process = [y for y in years_to_process if y >= start_year]
    if end_year:
        years_to_process = [y for y in years_to_process if y <= end_year]

    print(
        f"\nImporting {len(years_to_process)} year(s) from {folder_path}\n"
        f"  Years: {years_to_process[0] if years_to_process else 'none'} "
        f"– {years_to_process[-1] if years_to_process else 'none'}\n"
        f"  Destination cache: {cache_db_path}\n",
        flush=True,
    )

    try:
        from tqdm import tqdm as _tqdm
        _have_tqdm = True
    except ImportError:
        _tqdm      = None
        _have_tqdm = False

    def _write(msg, end="\n"):
        if _have_tqdm:
            _tqdm.write(msg, end=end)
        else:
            print(msg, end=end, flush=True)

    total_records = 0
    total_sats = set()
    years_done = 0

    # Flatten to a list of all parts to show an outer file-count bar
    all_parts = []
    for year in years_to_process:
        for part in sorted(by_year[year], key=lambda x: x[0]):
            all_parts.append((year, *part))   # (year, part_num, total_parts, fpath)

    outer_bar = None
    if _have_tqdm:
        outer_bar = _tqdm(
            total=len(all_parts),
            unit=" file",
            desc="Overall",
            dynamic_ncols=True,
            bar_format=(
                "Overall |{bar}| {n_fmt}/{total_fmt} files "
                "[{elapsed}<{remaining}]"
            ),
            position=1,
            leave=True,
        )

    for year in years_to_process:
        parts = sorted(by_year[year], key=lambda x: x[0])
        n_parts = len(parts)

        cov_start = date(year, 1, 1)
        cov_end   = date(year, 12, 31)

        year_records = 0
        year_sats = set()

        for part_num, total_parts, fpath in parts:
            fname = os.path.basename(fpath)
            part_label = f" ({part_num}/{total_parts})" if total_parts > 1 else ""
            size_mb = os.path.getsize(fpath) / (1024 * 1024)

            if n_parts == 1:
                out_zip = os.path.join(
                    archive_dir(tle_data_dir), str(year), f"{year}.zip"
                )
            else:
                out_zip = os.path.join(
                    archive_dir(tle_data_dir), str(year),
                    f"{year}-part{part_num:02d}of{total_parts:02d}.zip"
                )

            if os.path.exists(out_zip):
                size_kb = os.path.getsize(out_zip) / 1024
                _write(
                    f"  {year}{part_label}: already imported "
                    f"({size_kb:.0f} KB) -- skipping"
                )
                if outer_bar:
                    outer_bar.update(1)
                continue

            _write(
                f"\n  {year}{part_label}: {fname} ({size_mb:.1f} MB)",
                end="\n",
            )

            t0 = time.time()
            try:
                with zipfile.ZipFile(fpath, "r") as zf:
                    inner_names = zf.namelist()
                    if not inner_names:
                        _write(f"  {year}{part_label}: empty zip -- skipping")
                        if outer_bar:
                            outer_bar.update(1)
                        continue
                    inner_name = inner_names[0]
                    line_iter = (
                        line.decode("utf-8", errors="replace")
                        for line in zf.open(inner_name)
                    )
                    os.makedirs(os.path.dirname(out_zip), exist_ok=True)
                    records, sats = _ingest_3le_text(
                        line_iter, cache_db_path, out_zip,
                        coverage_start=cov_start,
                        coverage_end=cov_end,
                        status_callback=status_callback,
                        progress_label=f"  {year}{part_label}",
                    )
            except Exception as e:
                _write(f"  {year}{part_label}: ERROR: {e}")
                if outer_bar:
                    outer_bar.update(1)
                continue

            elapsed = time.time() - t0
            rate = records / elapsed if elapsed > 0 else 0
            _write(
                f"  {year}{part_label}: {records:,} records, "
                f"{len(sats):,} sats — {elapsed:.0f}s "
                f"({rate:,.0f} rec/s)"
            )
            year_records += records
            year_sats.update(sats)
            if outer_bar:
                outer_bar.update(1)

        if n_parts > 1:
            _write(
                f"  {year} total: {year_records:,} records, "
                f"{len(year_sats):,} unique satellites"
            )

        total_records += year_records
        total_sats.update(year_sats)
        years_done += 1

    if outer_bar:
        outer_bar.close()

    print(
        f"\nImport complete:\n"
        f"  {years_done} year(s) processed\n"
        f"  {total_records:,} total TLE records\n"
        f"  {len(total_sats):,} unique satellites\n"
        f"  Cache: {cache_db_path}",
        flush=True,
    )

    return {
        "years_processed": years_done,
        "total_records":   total_records,
        "total_sats":      len(total_sats),
    }


def archive_dir(tle_data_dir):
    """
    Root directory for all zip archives.
    This IS the TLE_DATA_DIR itself -- year subfolders sit directly
    inside it alongside the original bulk zip files, so all TLE data
    is in one unified location for backup and portability.
    """
    return tle_data_dir


def year_dir(tle_data_dir, year):
    """Path for a specific year's subfolder inside the TLE data directory."""
    d = os.path.join(tle_data_dir, str(year))
    os.makedirs(d, exist_ok=True)
    return d


def monthly_zip_path(tle_data_dir, year, month):
    """Path for a monthly archive zip inside the year subfolder."""
    if month == 0:
        # Convenience: month=0 means a full-year zip
        return os.path.join(year_dir(tle_data_dir, year), f"{year}.zip")
    return os.path.join(year_dir(tle_data_dir, year), f"{year}-{month:02d}.zip")


def daily_zip_path(tle_data_dir, day: date):
    """Path for a daily GP snapshot zip inside the year subfolder."""
    return os.path.join(year_dir(tle_data_dir, day.year), f"{day.isoformat()}.zip")


# ──────────────────────────────────────────────────────────────────────
# Internal: parse a 3LE text block into (norad_id, epoch_iso, raw_3le)
# ──────────────────────────────────────────────────────────────────────

def rollup_year_to_zip(tle_data_dir, year, dry_run=False):
    """
    Merge all individual daily and monthly zip files for a given year
    into a single year-packet zip (YYYY/YYYY.zip), then delete the
    individual files to reclaim space and keep the folder clean.

    Why this matters: daily GP snapshots accumulate one zip per day
    (365+ files/year at ~5MB each uncompressed, ~2MB compressed).
    Left unchecked, a year of daily captures adds up to ~700MB of
    individual zips. Rolling them into one year-packet compresses
    the combined TLE data 3-5x further (TLE text is highly repetitive
    across days), reduces it to one file to back up, and keeps the
    year subfolder from getting unwieldy.

    The rollup is safe to run at any time during the year -- it merges
    whatever daily files exist so far and leaves the year zip ready
    for subsequent daily additions. Running it again after more days
    accumulate will add those new days to the existing year zip.

    dry_run=True: print what WOULD be done without writing anything,
    for previewing before committing.

    Returns a summary dict: {merged_files, records_in_rollup, zip_size_mb,
    space_freed_mb, skipped_because_current_year}.
    """
    from datetime import date as _date

    ydir = os.path.join(tle_data_dir, str(year))
    if not os.path.isdir(ydir):
        print(f"  No data found for {year} in {tle_data_dir}", flush=True)
        return {"merged_files": 0, "skipped_because_no_data": True}

    # Find all daily (YYYY-MM-DD.zip) and monthly (YYYY-MM.zip) files
    # in the year folder. Exclude the year-packet itself (YYYY.zip) and
    # any original Space-Track bulk files (tle*.zip at the root level).
    import re as _re
    daily_pat   = _re.compile(rf"^{year}-\d{{2}}-\d{{2}}\.zip$")
    monthly_pat = _re.compile(rf"^{year}-\d{{2}}\.zip$")

    to_merge = []
    for fname in sorted(os.listdir(ydir)):
        if daily_pat.match(fname) or monthly_pat.match(fname):
            to_merge.append(os.path.join(ydir, fname))

    if not to_merge:
        print(f"  No daily/monthly files found for {year} -- nothing to roll up.", flush=True)
        return {"merged_files": 0, "skipped_because_no_data": True}

    total_src_bytes = sum(os.path.getsize(f) for f in to_merge)
    year_zip_path = os.path.join(ydir, f"{year}.zip")
    existing_year_zip = os.path.exists(year_zip_path)

    print(
        f"{'[DRY RUN] ' if dry_run else ''}"
        f"Rolling up {year}: {len(to_merge)} file(s) "
        f"({total_src_bytes/1024/1024:.1f} MB) → {year}.zip"
        + (f" (merging with existing {year}.zip)" if existing_year_zip else ""),
        flush=True,
    )

    if dry_run:
        for f in to_merge:
            print(f"  would merge: {os.path.basename(f)}", flush=True)
        return {"merged_files": len(to_merge), "dry_run": True}

    # Read existing year zip entries first (if it already exists),
    # then add all new daily files, deduplicating by inner filename.
    existing_entries = {}  # inner_name -> bytes
    if existing_year_zip:
        try:
            with zipfile.ZipFile(year_zip_path, "r") as zf:
                for name in zf.namelist():
                    existing_entries[name] = zf.read(name)
        except Exception as e:
            print(f"  Warning: could not read existing {year}.zip: {e}", flush=True)

    # Read each source zip and collect its inner files
    new_entries = {}
    records_count = 0
    for src_zip in to_merge:
        try:
            with zipfile.ZipFile(src_zip, "r") as zf:
                for name in zf.namelist():
                    content = zf.read(name)
                    # Use date-prefixed inner name to avoid collisions
                    inner_name = f"{os.path.splitext(os.path.basename(src_zip))[0]}_{name}"
                    new_entries[inner_name] = content
                    records_count += content.count(b"\n1 ")
        except Exception as e:
            print(f"  Warning: skipping {os.path.basename(src_zip)}: {e}", flush=True)

    # Write the merged year zip (existing entries + new entries)
    all_entries = {**existing_entries, **new_entries}
    tmp_path = year_zip_path + ".tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for inner_name, content in sorted(all_entries.items()):
                zf.writestr(inner_name, content)

        # Atomic replace
        if existing_year_zip:
            os.unlink(year_zip_path)
        os.rename(tmp_path, year_zip_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise RuntimeError(f"Failed to write {year}.zip: {e}") from e

    final_size = os.path.getsize(year_zip_path)

    # Remove the individual source files now that they're in the year zip
    removed = 0
    for src_zip in to_merge:
        try:
            os.unlink(src_zip)
            removed += 1
        except Exception as e:
            print(f"  Warning: could not remove {os.path.basename(src_zip)}: {e}", flush=True)

    space_freed = total_src_bytes - final_size
    print(
        f"  Done: {removed} file(s) merged → {year}.zip "
        f"({final_size/1024/1024:.1f} MB, "
        f"freed {space_freed/1024/1024:.1f} MB)",
        flush=True,
    )

    return {
        "merged_files":   removed,
        "zip_size_mb":    final_size / 1024 / 1024,
        "space_freed_mb": space_freed / 1024 / 1024,
        "skipped_because_no_data": False,
    }


def rollup_all_complete_years(tle_data_dir, dry_run=False):
    """
    Roll up all COMPLETE years (any year before the current calendar year)
    found in the TLE data directory. The current year is never auto-rolled
    since daily files for it are still accumulating.

    Safe to call from a scheduled task (e.g. January 1st each year) --
    it only processes years that are definitively finished, skips years
    with no data, and skips years already fully rolled up (no
    individual daily files remaining).

    Returns a list of per-year summary dicts from rollup_year_to_zip().
    """
    from datetime import date as _date
    current_year = _date.today().year

    if not os.path.isdir(tle_data_dir):
        return []

    results = []
    for entry in sorted(os.listdir(tle_data_dir)):
        ypath = os.path.join(tle_data_dir, entry)
        if not os.path.isdir(ypath):
            continue
        try:
            year = int(entry)
        except ValueError:
            continue
        if year >= current_year:
            continue  # Don't roll up the current year
        result = rollup_year_to_zip(tle_data_dir, year, dry_run=dry_run)
        results.append((year, result))

    return results
    """
    Auto-detect whether a block of TLE text is in 2LE or 3LE format.

    2LE (two-line element set, no name): every record is exactly two lines
    -- line 1 starting with '1 ' followed by line 2 starting with '2 '.
    This is the format used in Space-Track's publicfiles bulk downloads
    (tle2024.txt.zip contains tle2024.txt in 2LE format).

    3LE (three-line element set, with name): every record is three lines
    -- a name/title line (often prefixed with '0 '), then line 1, then line 2.
    This is the format returned by Space-Track's gp_history API.

    Returns 'tle2' or 'tle3'. If uncertain, defaults to 'tle2' since that
    is what the bulk files use and the parser handles both safely.
    """
    non_blank = [l.rstrip("\r\n") for l in lines if l.strip()]
    if len(non_blank) < 2:
        return "tle2"
    # Sample the first few non-blank lines
    for i in range(min(len(non_blank) - 1, 20)):
        if non_blank[i].startswith("1 ") and non_blank[i+1].startswith("2 "):
            # The line BEFORE this pair is a name line if it doesn't start
            # with '1 ' or '2 ' -- that's 3LE
            if i > 0 and not non_blank[i-1].startswith("1 ") and not non_blank[i-1].startswith("2 "):
                return "tle3"
            return "tle2"
    return "tle2"


def _parse_tle_stream(text_iter):
    """
    Yield (norad_id, epoch_iso, line1, line2) tuples from an iterator of
    raw text lines, handling both 2LE and 3LE format transparently.

    Processes one line at a time -- does NOT load all lines into memory
    first. This is critical for large files (e.g. the 1.1 GB 2004 part 7
    file) where calling list(text_iter) would allocate hundreds of MB just
    to hold the lines before parsing begins.
    """
    pending = None   # last seen candidate line-1

    for raw_line in text_iter:
        line = raw_line.rstrip("\r\n") if isinstance(raw_line, str) \
               else raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")

        if not line.strip():
            continue

        if line.startswith("1 ") and len(line) >= 60:
            pending = line

        elif line.startswith("2 ") and len(line) >= 60 and pending is not None:
            try:
                norad_id = int(pending[2:7].strip())
                epoch_iso = _parse_tle_epoch(pending)
                yield norad_id, epoch_iso, pending, line
            except Exception:
                pass
            pending = None

        else:
            # Name line (3LE) or anything else -- don't clear pending,
            # because the very next line might still be a valid line-1.
            if not line.startswith("2 "):
                pending = None


# Keep the old name as an alias so existing callers don't break
def _parse_3le_stream(text_iter):
    """Alias for _parse_tle_stream -- handles both 2LE and 3LE transparently."""
    yield from _parse_tle_stream(text_iter)


def _parse_tle_epoch(line1):
    """
    Parse the epoch field from TLE line 1 into a naive UTC ISO string.
    Field: columns 19-32  format: YYDDD.DDDDDDDD
    """
    epoch_str = line1[18:32].strip()
    yy = int(epoch_str[0:2])
    year = 2000 + yy if yy < 57 else 1900 + yy
    day_frac = float(epoch_str[2:])
    dt = datetime(year, 1, 1) + timedelta(days=day_frac - 1)
    return dt.isoformat()


def _parse_orbital_elements(line1, line2):
    """
    Extract the orbital elements needed by tle_history_cache from line1/line2.
    Returns dict with altitude_km (approximated from mean motion), inclination_deg,
    eccentricity, period_min.
    """
    try:
        inclination_deg = float(line2[8:16].strip())
        eccentricity = float("0." + line2[26:33].strip())
        mean_motion = float(line2[52:63].strip())  # rev/day
        period_min = (1440.0 / mean_motion) if mean_motion > 0 else 0
        # Approximate altitude from mean motion via Kepler's third law
        mu = 398600.4418
        a = (mu / ((mean_motion * 2 * 3.14159265 / 86400) ** 2)) ** (1/3)
        altitude_km = a - 6378.135
        return {
            "inclination_deg": inclination_deg,
            "eccentricity":    eccentricity,
            "period_min":      period_min,
            "altitude_km":     altitude_km,
        }
    except Exception:
        return {
            "inclination_deg": None,
            "eccentricity":    None,
            "period_min":      None,
            "altitude_km":     None,
        }


# ──────────────────────────────────────────────────────────────────────
# Core: ingest 3LE text into both SQLite cache and zip archive
# ──────────────────────────────────────────────────────────────────────

def _ingest_3le_text(text_or_iter, cache_db_path, zip_path,
                      coverage_start, coverage_end,
                      batch_size=100000, status_callback=None,
                      progress_label=""):
    """
    Parse TLE text (or a line iterator) and write records to:
      1. The SQLite TLE history cache via BulkImportSession
      2. A zip archive (ZIP_STORED for speed during import)

    Shows a live tqdm progress bar with records/sec and running total.
    Returns (records_written, norad_ids_seen).
    """
    import tle_history_cache

    try:
        from tqdm import tqdm
        _have_tqdm = True
    except ImportError:
        _have_tqdm = False

    if isinstance(text_or_iter, str):
        lines_iter = iter(text_or_iter.splitlines())
    else:
        lines_iter = text_or_iter

    os.makedirs(
        os.path.dirname(zip_path) if os.path.dirname(zip_path) else ".",
        exist_ok=True
    )
    zip_name = os.path.basename(zip_path).replace(".zip", ".2le")

    rows_buf = []

    # Progress bar: unit = TLE records, shows rate (rec/s) and total
    bar = None
    if _have_tqdm:
        bar = tqdm(
            unit=" rec",
            unit_scale=True,
            unit_divisor=1000,
            desc=progress_label or "  importing",
            dynamic_ncols=True,
            bar_format=(
                "{desc}: {n_fmt} records "
                "[{elapsed}<{remaining}, {rate_fmt}]"
            ),
            miniters=batch_size // 10,  # update at most 10x per batch
        )

    with tle_history_cache.BulkImportSession(
        cache_db_path, coverage_start, coverage_end
    ) as sess:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            with zf.open(zip_name, "w") as arc:
                for norad_id, epoch_iso, l1, l2 in _parse_tle_stream(lines_iter):
                    elems = _parse_orbital_elements(l1, l2)
                    rows_buf.append((
                        norad_id, epoch_iso,
                        elems["altitude_km"], elems["inclination_deg"],
                        elems["eccentricity"], elems["period_min"],
                    ))
                    arc.write((l1 + "\n").encode())
                    arc.write((l2 + "\n").encode())
                    arc.write(b"\n")

                    if len(rows_buf) >= batch_size:
                        sess.insert_batch(rows_buf)
                        if bar:
                            bar.update(len(rows_buf))
                        if status_callback:
                            status_callback(sess.total_inserted)
                        rows_buf.clear()

                if rows_buf:
                    sess.insert_batch(rows_buf)
                    if bar:
                        bar.update(len(rows_buf))
                    if status_callback:
                        status_callback(sess.total_inserted)

    if bar:
        bar.close()

    return sess.total_inserted, sess.norad_ids_seen


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def bulk_seed_from_gp_history(session, cache_db_path, tle_data_dir,
                               start_year, start_month,
                               end_year, end_month,
                               rate_limiter=None, timeout_sec=300,
                               request_log_db=None):
    """
    One-time historical TLE seeding via date-range gp_history queries.

    Fetches one month at a time from Space-Track's gp_history class,
    using EPOCH/{start}--{end} date-range filtering rather than per-
    satellite NORAD_CAT_ID filtering. This is the approach Space-Track
    recommends for large historical pulls ("download TLEs bundled as zip
    files by year" in their documentation refers to accumulating data
    this way, not a pre-existing public download URL).

    ONE REQUEST PER MONTH -- for 3 years that's 36 total requests,
    logged to the api_request_log under CLASS_GP_HISTORY so the pre-
    flight check accounts for them. Each response contains ALL satellites
    updated in that month window, not just a specific satellite subset.

    Data is simultaneously:
    • Parsed on the fly (streaming, low memory)
    • Inserted into the SQLite TLE history cache
    • Written to a compressed monthly zip archive (data/tle_archive/YYYY/YYYY-MM.zip)

    The zip archives are a backup: if the cache is ever lost, call
    import_from_zips() to rebuild it without re-querying Space-Track.

    start_year/start_month, end_year/end_month: inclusive range of
    months to fetch. Months whose zip archive already exists are
    SKIPPED entirely -- making this safe to resume after an interruption
    without re-downloading anything.

    Returns a summary dict with total records ingested.
    """
    import api_request_log

    # Build list of (year, month) slices, skipping already-archived months
    slices = []
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        zp = monthly_zip_path(tle_data_dir, y, m)
        if os.path.exists(zp):
            print(f"  {y}-{m:02d}: already archived at {zp} -- skipping", flush=True)
        else:
            slices.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    if not slices:
        print("  All months already archived -- nothing to fetch.", flush=True)
        return {"months_fetched": 0, "total_records": 0, "skipped": True}

    print(
        f"\nBulk historical seed: {len(slices)} month(s) to fetch "
        f"({slices[0][0]}-{slices[0][1]:02d} through "
        f"{slices[-1][0]}-{slices[-1][1]:02d})\n"
        f"Each month = 1 gp_history request covering ALL satellites.\n"
        f"Existing monthly archives will not be re-downloaded.\n",
        flush=True,
    )

    total_records = 0
    total_sats = set()

    for idx, (year, month) in enumerate(slices, 1):
        # Compute inclusive date range for this month
        month_start = date(year, month, 1)
        if month == 12:
            month_end = date(year, 12, 31)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)

        epoch_range = f"{month_start.isoformat()}--{month_end.isoformat()}"

        url = (
            f"{BASICSPACEDATA}/class/gp_history"
            f"/EPOCH/{epoch_range}"
            f"/orderby/EPOCH"
            f"/format/3le"
            f"/emptyresult/show"
        )

        print(
            f"  [{idx}/{len(slices)}] {year}-{month:02d} "
            f"({month_start} → {month_end})...",
            end=" ", flush=True,
        )

        if rate_limiter is not None:
            rate_limiter.acquire(
                request_class="gp_history",
                norad_count=0,  # unknown count for date-range queries
            )

        if request_log_db:
            api_request_log.log_request(
                request_log_db,
                api_request_log.CLASS_GP_HISTORY,
                norad_count=0,
            )

        t0 = time.time()
        try:
            resp = session.get(url, timeout=timeout_sec, stream=True)
            resp.raise_for_status()

            # Collect streamed content -- we need the full text to pass
            # to the parser, but we chunk it to avoid blocking the
            # connection for the full response duration.
            chunks = []
            for chunk in resp.iter_content(chunk_size=65536, decode_unicode=False):
                if chunk:
                    chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8", errors="replace")
        except Exception as e:
            print(f"FAILED ({e})", flush=True)
            print(
                f"    This month's data was not saved. The seed can be\n"
                f"    resumed by running bulk_seed_from_gp_history() again --\n"
                f"    months with an existing zip file are automatically skipped.",
                flush=True,
            )
            continue

        elapsed_dl = time.time() - t0

        if not text.strip():
            print(f"empty response ({elapsed_dl:.1f}s) -- month may have no data", flush=True)
            # Write an empty zip so we don't re-request this month
            zp = monthly_zip_path(tle_data_dir, year, month)
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(f"{year}-{month:02d}.3le", "")
            continue

        zp = monthly_zip_path(tle_data_dir, year, month)
        records, sats = _ingest_3le_text(
            text, cache_db_path, zp,
            coverage_start=month_start,
            coverage_end=month_end,
        )
        elapsed_total = time.time() - t0

        zip_size_kb = os.path.getsize(zp) / 1024 if os.path.exists(zp) else 0
        print(
            f"{records:,} records, {len(sats):,} satellites "
            f"({elapsed_dl:.0f}s download, {elapsed_total:.0f}s total, "
            f"zip: {zip_size_kb:.0f} KB)",
            flush=True,
        )
        total_records += records
        total_sats.update(sats)

    print(
        f"\nBulk seed complete: {total_records:,} total TLE records for "
        f"{len(total_sats):,} unique satellites.",
        flush=True,
    )
    return {
        "months_fetched":  len(slices),
        "total_records":   total_records,
        "unique_sats":     len(total_sats),
        "skipped":         False,
    }


def snapshot_daily_gp(session, cache_db_path, tle_data_dir,
                       snapshot_date=None, rate_limiter=None,
                       timeout_sec=120, request_log_db=None,
                       username=None, password=None):
    """
    Download today's full GP catalog (all on-orbit objects, one request)
    and add it to the TLE history cache and daily zip archive.

    Uses Space-Track's GP class (NOT gp_history) -- this is under the
    GP: 1/hour policy, completely separate from the strict gp_history
    lifetime limit. One call per day, covers every currently-tracked
    object, and accumulates into a growing multi-year history over time.

    If username/password are provided and the spacetrack library is
    installed (pip install spacetrack), the response is streamed line-
    by-line instead of buffered in memory -- reducing peak RAM from
    ~50-100 MB to <1 MB for the GP catalog download.

    snapshot_date: the date to label this snapshot (default: today UTC).
    If the zip already exists, the snapshot is skipped (idempotent).

    Returns (records_written, skipped_because_already_exists).
    """
    import api_request_log

    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).replace(tzinfo=None).date()

    zp = daily_zip_path(tle_data_dir, snapshot_date)
    if os.path.exists(zp):
        print(
            f"  Daily GP snapshot for {snapshot_date} already exists "
            f"at {zp} -- skipping.",
            flush=True,
        )
        return 0, True

    print(
        f"Daily GP snapshot {snapshot_date}: fetching full catalog...",
        end=" ", flush=True,
    )

    t0 = time.time()
    text = None

    # Try streaming via the spacetrack library first (lower memory footprint)
    if username and password:
        try:
            import spacetrack_client as _stc
            if _stc._lib_available:
                lines = _stc.fetch_daily_gp_streaming(
                    username, password,
                    rate_limiter=rate_limiter,
                    timeout_sec=timeout_sec,
                    request_log_db=request_log_db,
                )
                if lines is not None:
                    text = "\n".join(lines)
        except Exception:
            text = None  # fall through to the standard approach

    # Standard approach: stream via requests.Session chunks
    if text is None:
        url = (
            f"{BASICSPACEDATA}/class/gp"
            f"/decay_date/null-val"
            f"/epoch/%3Enow-30"
            f"/orderby/NORAD_CAT_ID"
            f"/format/3le"
            f"/emptyresult/show"
        )
        if rate_limiter is not None:
            rate_limiter.acquire(request_class="gp", norad_count=0)
        if request_log_db:
            api_request_log.log_request(request_log_db, "gp", norad_count=0)
        try:
            resp = session.get(url, timeout=timeout_sec, stream=True)
            resp.raise_for_status()
            chunks = []
            for chunk in resp.iter_content(chunk_size=65536, decode_unicode=False):
                if chunk:
                    chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8", errors="replace")
        except Exception as e:
            print(f"FAILED ({e})", flush=True)
            return 0, False

    elapsed = time.time() - t0
    if not text or not text.strip():
        print(f"empty response ({elapsed:.1f}s)", flush=True)
        return 0, False

    records, sats = _ingest_3le_text(
        text, cache_db_path, zp,
        coverage_start=snapshot_date,
        coverage_end=snapshot_date,
    )
    zip_size_kb = os.path.getsize(zp) / 1024 if os.path.exists(zp) else 0
    print(
        f"{records:,} records, {len(sats):,} satellites "
        f"({elapsed:.1f}s, zip: {zip_size_kb:.0f} KB)",
        flush=True,
    )
    return records, False


def import_from_zips(cache_db_path, tle_data_dir, status_callback=None):
    """
    Re-import TLE data from all local zip archives into the SQLite cache,
    without making any network requests.

    Use this to rebuild a lost or corrupted cache from the local
    archives. Processes archives in chronological order (oldest first)
    so coverage records in the cache accurately reflect what's been
    loaded. Monthly archives (YYYY-MM.zip) and daily snapshots
    (YYYY-MM-DD.zip) are both handled.

    Returns the total number of records imported.
    """
    root = tle_data_dir
    if not os.path.exists(root):
        print(f"No archive directory found at {root}.", flush=True)
        return 0

    # Collect all zip files, sort chronologically by filename
    all_zips = []
    for year_dir in sorted(os.listdir(root)):
        year_path = os.path.join(root, year_dir)
        if not os.path.isdir(year_path):
            continue
        for fname in sorted(os.listdir(year_path)):
            if fname.endswith(".zip"):
                all_zips.append(os.path.join(year_path, fname))

    if not all_zips:
        print("No zip archives found.", flush=True)
        return 0

    print(
        f"Re-importing from {len(all_zips)} zip archive(s)...",
        flush=True,
    )

    total = 0
    for zp in all_zips:
        fname = os.path.basename(zp)
        date_part = fname.replace(".zip", "")
        # Determine coverage dates from filename (YYYY-MM or YYYY-MM-DD)
        try:
            parts = date_part.split("-")
            if len(parts) == 2:
                year, month = int(parts[0]), int(parts[1])
                cov_start = date(year, month, 1)
                if month == 12:
                    cov_end = date(year, 12, 31)
                else:
                    cov_end = date(year, month + 1, 1) - timedelta(days=1)
            elif len(parts) == 3:
                cov_start = date(int(parts[0]), int(parts[1]), int(parts[2]))
                cov_end = cov_start
            else:
                print(f"  Skipping unrecognized filename: {fname}", flush=True)
                continue
        except Exception:
            print(f"  Skipping unrecognized filename: {fname}", flush=True)
            continue

        try:
            with zipfile.ZipFile(zp, "r") as zf:
                names = zf.namelist()
                if not names:
                    continue
                text = zf.read(names[0]).decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  {fname}: could not read ({e})", flush=True)
            continue

        if not text.strip():
            continue

        # We don't write a zip (it already exists), so pass a temp path
        # that we'll discard. Use /dev/null equivalent.
        tmp_zip = zp + ".reimport_tmp"
        try:
            records, sats = _ingest_3le_text(
                text, cache_db_path, tmp_zip,
                coverage_start=cov_start,
                coverage_end=cov_end,
                status_callback=status_callback,
            )
        finally:
            if os.path.exists(tmp_zip):
                os.unlink(tmp_zip)

        print(
            f"  {fname}: {records:,} records, {len(sats):,} satellites",
            flush=True,
        )
        total += records

    print(f"\nImport complete: {total:,} total records.", flush=True)
    return total


def archive_summary(tle_data_dir):
    """
    Return a human-readable summary of what's in the TLE data directory.
    Counts all zip file types: original bulk downloads, year packets,
    monthly archives, daily snapshots, and delta sync files.
    """
    import re as _re

    if not os.path.exists(tle_data_dir):
        return f"TLE data directory not found: {tle_data_dir}"

    bulk_orig = 0    # tle2024.txt.zip  (original Space-Track downloads)
    year_pkts = 0    # 2024/2024.zip    (rolled-up year packets)
    monthly   = 0    # 2024/2024-03.zip
    daily     = 0    # 2024/2024-03-15.zip
    delta     = 0    # 2024/2024-03-15-delta-*.zip
    total_kb  = 0.0

    # Count original bulk files in the root
    bulk_pat = _re.compile(r"^tle\d{4}.*\.zip$", _re.IGNORECASE)
    for fname in os.listdir(tle_data_dir):
        fp = os.path.join(tle_data_dir, fname)
        if os.path.isfile(fp) and bulk_pat.match(fname):
            bulk_orig += 1
            total_kb += os.path.getsize(fp) / 1024

    # Count year subfolders
    for entry in os.listdir(tle_data_dir):
        yp = os.path.join(tle_data_dir, entry)
        if not os.path.isdir(yp):
            continue
        try:
            int(entry)  # must be a 4-digit year folder
        except ValueError:
            continue
        for fname in os.listdir(yp):
            if not fname.endswith(".zip"):
                continue
            fp = os.path.join(yp, fname)
            total_kb += os.path.getsize(fp) / 1024
            base = fname.replace(".zip", "")
            if "delta" in base:
                delta += 1
            elif _re.match(r"^\d{4}-\d{2}-\d{2}$", base):
                daily += 1
            elif _re.match(r"^\d{4}-\d{2}$", base):
                monthly += 1
            elif _re.match(r"^\d{4}(-.+)?$", base):
                year_pkts += 1

    parts = []
    if bulk_orig:  parts.append(f"{bulk_orig} original bulk file(s)")
    if year_pkts:  parts.append(f"{year_pkts} year packet(s)")
    if monthly:    parts.append(f"{monthly} monthly file(s)")
    if daily:      parts.append(f"{daily} daily snapshot(s)")
    if delta:      parts.append(f"{delta} delta file(s)")

    if not parts:
        return f"TLE data directory exists but contains no zip archives: {tle_data_dir}"

    return (
        f"TLE archives ({tle_data_dir}):\n"
        f"  {', '.join(parts)}\n"
        f"  {total_kb/1024:.1f} MB total on disk"
    )

# ──────────────────────────────────────────────────────────────────────
# Strategy 1: Bulk seed from Space-Track's publicfiles cloud storage
# ──────────────────────────────────────────────────────────────────────

def list_publicfiles_zips(session, timeout_sec=30):
    """
    Query Space-Track's publicfiles API to discover what TLE zip files
    are available on their cloud storage site (Sync.com).

    Per the official documentation for gp_history:
      "For queries of many objects or large date ranges, download TLEs
       bundled as zip files by year from our cloud storage site instead."

    The publicfiles API returns metadata (SOURCE, TYPE, DATE, LINK, SIZE)
    for each available file. This function filters for TLE-type files
    and returns them sorted by date, oldest first, so the caller can
    download them in chronological order.

    Returns a list of dicts: [{source, type, date, link, name, size}, ...]
    Returns [] on any error (caller should fall back to other strategies).
    """
    try:
        resp = session.get(PUBLICFILES_INFO, timeout=timeout_sec)
        resp.raise_for_status()
        files = json.loads(resp.text)
        # Filter for TLE data files only, exclude non-TLE types
        tle_files = [
            f for f in files
            if "TLE" in str(f.get("TYPE", "")).upper()
            or "GP" in str(f.get("TYPE", "")).upper()
            or f.get("NAME", "").endswith(".zip")
        ]
        # Sort chronologically (oldest first for systematic seeding)
        tle_files.sort(key=lambda f: f.get("DATE", ""))
        return tle_files
    except Exception as e:
        print(f"  publicfiles listing failed: {e}", flush=True)
        return []


def seed_from_publicfiles(session, cache_db_path, tle_data_dir,
                           start_year=None, end_year=None,
                           rate_limiter=None, timeout_sec=600,
                           request_log_db=None):
    """
    Seed the local TLE history cache from Space-Track's publicfiles
    cloud storage (the Files Panel bulk zip downloads).

    This is the approach documented in Space-Track's gp_history class
    description: "download TLEs bundled as zip files by year from our
    cloud storage site instead." The Files Panel policy is 1/lifetime
    per file -- download each year's zip ONCE and store it locally.

    Each year's zip is:
    1. Downloaded from Space-Track's publicfiles endpoint
    2. Saved to data/tle_archive/YYYY/YYYY-publicfiles.zip
    3. Its contents parsed and loaded into the SQLite TLE history cache

    Files already present locally (same filename) are SKIPPED -- making
    this safe to resume after interruption and idempotent.

    start_year / end_year: filter which years to download (default: all
    available). Set start_year=date.today().year-3 to get 3 years only.

    Returns summary dict.
    """
    import api_request_log

    print("\nDiscovering available TLE zip files from Space-Track publicfiles...")
    available = list_publicfiles_zips(session, timeout_sec=30)

    if not available:
        print(
            "  No TLE zip files found via publicfiles API.\n"
            "  Either none are available for your account, or the listing\n"
            "  failed. Falling back to gp_history date-range queries."
        )
        return {"files_downloaded": 0, "records_ingested": 0, "fallback_needed": True}

    # Filter by year range if specified
    if start_year or end_year:
        filtered = []
        for f in available:
            try:
                year = int(f.get("DATE", "0")[:4])
                if start_year and year < start_year:
                    continue
                if end_year and year > end_year:
                    continue
                filtered.append(f)
            except Exception:
                filtered.append(f)
        available = filtered

    print(f"  Found {len(available)} TLE file(s) available for download.")
    for f in available:
        print(f"    {f.get('NAME', '?')} ({f.get('SIZE', '?')}, {f.get('DATE', '?')})")
    print()

    total_records = 0
    files_downloaded = 0

    for pf in available:
        link = pf.get("LINK", "")
        name = pf.get("NAME", link.split("/")[-1] if "/" in link else link)
        file_date = pf.get("DATE", "")

        # Determine year for archive path
        try:
            year = int(file_date[:4]) if file_date else 0
        except Exception:
            year = 0

        # Local archive path -- store as-received, named by source file
        local_dir = os.path.join(tle_data_dir, str(year) if year else "publicfiles")
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, name if name.endswith(".zip") else name + ".zip")

        if os.path.exists(local_path):
            print(f"  {name}: already downloaded at {local_path} -- skipping", flush=True)
            continue

        print(f"  Downloading {name} ({pf.get('SIZE', '?')})...", end=" ", flush=True)

        if rate_limiter:
            rate_limiter.acquire(request_class="other")

        if request_log_db:
            api_request_log.log_request(request_log_db, "publicfiles")

        t0 = time.time()
        try:
            dl_url = f"{PUBLICFILES_DL}?name={link}"
            resp = session.get(dl_url, timeout=timeout_sec, stream=True)
            resp.raise_for_status()

            # Stream to local file to avoid holding large zip in memory
            with open(local_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        except Exception as e:
            print(f"FAILED ({e})", flush=True)
            if os.path.exists(local_path):
                os.unlink(local_path)
            continue

        elapsed = time.time() - t0
        size_mb = os.path.getsize(local_path) / (1024 * 1024)
        print(f"{size_mb:.1f} MB in {elapsed:.0f}s", flush=True)

        # Ingest the zip contents into the SQLite cache
        print(f"  Ingesting {name} into local cache...", end=" ", flush=True)
        t1 = time.time()
        records = _ingest_publicfiles_zip(local_path, cache_db_path, year)
        elapsed2 = time.time() - t1
        print(f"{records:,} records in {elapsed2:.0f}s", flush=True)

        total_records += records
        files_downloaded += 1

    print(
        f"\nPublicfiles seed complete: {files_downloaded} file(s) downloaded, "
        f"{total_records:,} records ingested.",
        flush=True,
    )
    return {
        "files_downloaded": files_downloaded,
        "records_ingested": total_records,
        "fallback_needed": False,
    }


def _ingest_publicfiles_zip(local_zip_path, cache_db_path, year):
    """
    Read a Space-Track publicfiles bulk zip and ingest its TLE records
    into the SQLite history cache. The zip may contain multiple TLE
    files (e.g. one per month, or one large file for the whole year).

    Returns total records ingested.
    """
    total = 0
    if year:
        cov_start = date(year, 1, 1)
        cov_end   = date(year, 12, 31)
    else:
        cov_start = date(2000, 1, 1)
        cov_end   = date.today()

    try:
        with zipfile.ZipFile(local_zip_path, "r") as zf:
            for name in zf.namelist():
                if not (name.endswith(".tle") or name.endswith(".txt") or
                        name.endswith(".3le") or name.endswith(".dat")):
                    continue
                text = zf.read(name).decode("utf-8", errors="replace")
                if not text.strip():
                    continue
                # Use a temp zip path (we already have the real local zip)
                tmp_zip = local_zip_path + ".tmp_ingest"
                try:
                    records, sats = _ingest_3le_text(
                        text, cache_db_path, tmp_zip,
                        coverage_start=cov_start, coverage_end=cov_end,
                    )
                    total += records
                finally:
                    if os.path.exists(tmp_zip):
                        os.unlink(tmp_zip)
    except Exception as e:
        print(f"  Warning: error reading {local_zip_path}: {e}", flush=True)

    return total


# ──────────────────────────────────────────────────────────────────────
# Strategy 2: Incremental delta sync via FILE predicate
# ──────────────────────────────────────────────────────────────────────

def _get_bookmark(cache_db_path):
    """
    Retrieve the last-seen FILE number bookmark from the cache DB.
    Returns 0 if no bookmark exists (first-time sync).
    """
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(cache_db_path, timeout=15)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_bookmark "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        row = conn.execute(
            "SELECT value FROM sync_bookmark WHERE key='last_file_number'"
        ).fetchone()
        conn.commit()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _set_bookmark(cache_db_path, file_number):
    """Store the last-seen FILE number bookmark in the cache DB."""
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(cache_db_path, timeout=15)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_bookmark "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO sync_bookmark (key, value) VALUES (?, ?)",
            ("last_file_number", str(file_number))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  Warning: could not save FILE bookmark: {e}", flush=True)


def sync_incremental_delta(session, cache_db_path, tle_data_dir,
                            norad_ids=None, rate_limiter=None,
                            timeout_sec=300, request_log_db=None):
    """
    Fetch only TLE records published after the last known FILE number,
    using the FILE predicate documented in Space-Track's How-To page.

    When no FILE bookmark exists (first delta sync after bulk import),
    uses an EPOCH date range instead -- fetching everything published
    in the past 18 months, which covers the gap between the bulk zip
    generation date and today without issuing a FILE > 0 query against
    220+ million records.

    Returns (records_fetched, new_bookmark).
    """
    import api_request_log

    last_file = _get_bookmark(cache_db_path)
    today     = date.today()

    if last_file == 0:
        # No bookmark -- use EPOCH range to get recent records
        # 18 months back covers the gap since bulk zips were generated
        epoch_start = date(today.year - 1, today.month, today.day) - \
                      __import__('datetime').timedelta(days=180)
        epoch_range = f"{epoch_start.isoformat()}--{today.isoformat()}"
        print(
            f"  No FILE bookmark -- fetching by EPOCH range "
            f"({epoch_start} → {today}) to cover gap since bulk import.",
            flush=True,
        )

        if norad_ids:
            id_list = ",".join(str(n) for n in norad_ids)
            url = (
                f"{BASICSPACEDATA}/class/gp_history"
                f"/NORAD_CAT_ID/{id_list}"
                f"/EPOCH/{epoch_range}"
                f"/orderby/EPOCH asc"
                f"/format/3le"
                f"/emptyresult/show"
            )
        else:
            url = (
                f"{BASICSPACEDATA}/class/gp_history"
                f"/EPOCH/{epoch_range}"
                f"/orderby/EPOCH asc"
                f"/format/json"
                f"/emptyresult/show"
            )
        query_label = f"EPOCH {epoch_start} → {today}"
    else:
        # Have a bookmark -- use FILE predicate for true incremental sync
        if norad_ids:
            id_list = ",".join(str(n) for n in norad_ids)
            url = (
                f"{BASICSPACEDATA}/class/gp_history"
                f"/NORAD_CAT_ID/{id_list}"
                f"/FILE/%3E{last_file}"
                f"/orderby/FILE asc"
                f"/format/3le"
                f"/emptyresult/show"
            )
        else:
            url = (
                f"{BASICSPACEDATA}/class/gp_history"
                f"/FILE/%3E{last_file}"
                f"/orderby/FILE asc"
                f"/format/json"
                f"/emptyresult/show"
            )
        query_label = f"FILE > {last_file}"

    print(
        f"Incremental delta sync: fetching gp_history ({query_label})...",
        end=" ", flush=True,
    )

    if rate_limiter:
        rate_limiter.acquire(request_class="gp_history")
    if request_log_db:
        api_request_log.log_request(request_log_db, "gp_history")

    t0 = time.time()
    try:
        resp = session.get(url, timeout=timeout_sec, stream=True)
        resp.raise_for_status()
        chunks = []
        for chunk in resp.iter_content(chunk_size=65536, decode_unicode=False):
            if chunk:
                chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"FAILED ({e})", flush=True)
        return 0, last_file

    elapsed = time.time() - t0

    if not text.strip():
        print(f"no new records ({elapsed:.1f}s)", flush=True)
        # Still get and store bookmark so future runs use FILE predicate
        new_file = _get_new_file_bookmark(session, norad_ids, timeout_sec)
        if new_file > 0:
            _set_bookmark(cache_db_path, new_file)
            print(f"  FILE bookmark set: {new_file}", flush=True)
        return 0, new_file

    delta_zip_path = os.path.join(
        archive_dir(tle_data_dir),
        str(today.year),
        f"{today.isoformat()}-delta.zip",
    )
    os.makedirs(os.path.dirname(delta_zip_path), exist_ok=True)

    records, sats = _ingest_3le_text(
        text, cache_db_path, delta_zip_path,
        coverage_start=date(2000, 1, 1),
        coverage_end=today,
    )
    print(
        f"{records:,} new records, {len(sats):,} satellites "
        f"({elapsed:.1f}s)",
        flush=True,
    )

    new_file = _get_new_file_bookmark(session, norad_ids, timeout_sec)
    if new_file > last_file:
        _set_bookmark(cache_db_path, new_file)
        print(f"  FILE bookmark updated: {last_file} → {new_file}", flush=True)
    elif new_file > 0 and last_file == 0:
        _set_bookmark(cache_db_path, new_file)
        print(f"  FILE bookmark set: {new_file}", flush=True)

    return records, new_file


def _get_new_file_bookmark(session, norad_ids=None, timeout_sec=30):
    """
    Get the current highest FILE number from Space-Track, to use as
    the bookmark for the next incremental sync. One lightweight query.
    """
    try:
        if norad_ids:
            id_list = ",".join(str(n) for n in norad_ids[:10])  # sample
            url = (
                f"{BASICSPACEDATA}/class/gp"
                f"/NORAD_CAT_ID/{id_list}"
                f"/predicates/FILE"
                f"/orderby/FILE desc"
                f"/limit/1"
                f"/format/json"
            )
        else:
            url = (
                f"{BASICSPACEDATA}/class/gp"
                f"/predicates/FILE"
                f"/orderby/FILE desc"
                f"/limit/1"
                f"/format/json"
            )
        resp = session.get(url, timeout=timeout_sec)
        resp.raise_for_status()
        data = json.loads(resp.text)
        if data and isinstance(data, list) and data[0].get("FILE"):
            return int(data[0]["FILE"])
    except Exception:
        pass
    return 0
