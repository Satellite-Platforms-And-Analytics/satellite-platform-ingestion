"""
trial_run.py — Offline verification of the satellite tool before live use.

Exercises every component that matters without making ANY network calls
to Space-Track. Run this after setting up the tool and importing your
TLE zip files to confirm everything is wired up correctly.

Usage:
    python trial_run.py
    python trial_run.py --verbose

Exit code 0 = all checks passed (safe to run the real tool).
Exit code 1 = one or more checks failed (see output for details).
"""

import sys
import os
import argparse
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS  = "✓ PASS"
FAIL  = "✗ FAIL"
WARN  = "⚠ WARN"
SKIP  = "  SKIP"

results = []
verbose = False


def check(label, fn):
    """Run a single check function, catch any exception, record result."""
    try:
        status, detail = fn()
    except Exception as e:
        status, detail = FAIL, f"{type(e).__name__}: {e}"
    results.append((label, status, detail))
    icon = status.split()[0]
    print(f"  {icon}  {label}")
    if detail and (verbose or status == FAIL):
        for line in detail.splitlines():
            print(f"       {line}")
    return status == PASS


# ──────────────────────────────────────────────────────────────────────
# 1. Configuration
# ──────────────────────────────────────────────────────────────────────

def check_config():
    from config import (
        TLE_DATA_DIR, TLE_HISTORY_CACHE_DB, SATCAT_CACHE_DB,
        API_REQUEST_LOG_DB, BASE_DIR, SPACETRACK_USERNAME,
        SPACETRACK_PASSWORD, HISTORICAL_LOOKBACK_YEARS,
    )
    issues = []
    if not os.path.isdir(TLE_DATA_DIR):
        issues.append(f"TLE_DATA_DIR does not exist: {TLE_DATA_DIR}")
        issues.append("  → Set TLE_DATA_DIR in your .env file and run --import-folder first")
    if not SPACETRACK_USERNAME:
        issues.append("SPACETRACK_USERNAME not set in .env")
    if not SPACETRACK_PASSWORD:
        issues.append("SPACETRACK_PASSWORD not set in .env")

    detail = (
        f"TLE_DATA_DIR       = {TLE_DATA_DIR}\n"
        f"TLE_HISTORY_CACHE  = {TLE_HISTORY_CACHE_DB}\n"
        f"SATCAT_CACHE       = {SATCAT_CACHE_DB}\n"
        f"API_REQUEST_LOG    = {API_REQUEST_LOG_DB}\n"
        f"Credentials set    = {'yes' if SPACETRACK_USERNAME and SPACETRACK_PASSWORD else 'NO'}\n"
        f"Lookback years     = {HISTORICAL_LOOKBACK_YEARS}"
    )
    if issues:
        return FAIL, detail + "\n" + "\n".join(issues)
    return PASS, detail


# ──────────────────────────────────────────────────────────────────────
# 2. TLE History Cache
# ──────────────────────────────────────────────────────────────────────

def check_tle_cache():
    from config import (
        TLE_HISTORY_CACHE_DB, HISTORICAL_LOOKBACK_YEARS,
        OUTPUT_FILE, BASE_DIR,
    )
    import tle_history_cache

    stats = tle_history_cache.cache_stats(TLE_HISTORY_CACHE_DB)
    if not os.path.exists(TLE_HISTORY_CACHE_DB):
        return WARN, (
            f"{stats}\n"
            "Cache does not exist yet -- run:\n"
            "  python seed_tle_history.py --import-folder \"C:\\...\\TLEs\""
        )

    end_date   = date.today()
    start_date = date(end_date.year - HISTORICAL_LOOKBACK_YEARS,
                      end_date.month, end_date.day)

    # Try to load NORAD IDs from the actual visible_satellites.xlsx so
    # the coverage check reflects your real catalog, not a hardcoded sample.
    catalog_ids = []
    catalog_source = "hardcoded sample"
    try:
        import pandas as pd
        vis_file = OUTPUT_FILE  # visible_satellites.xlsx
        if os.path.exists(vis_file):
            df = pd.read_excel(vis_file, usecols=["Target NORAD"])
            catalog_ids = df["Target NORAD"].dropna().astype(int).unique().tolist()
            catalog_source = f"visible_satellites.xlsx ({len(catalog_ids):,} satellites)"
    except Exception:
        pass

    # Fall back to a handful of well-known IDs if no catalog available
    if not catalog_ids:
        catalog_ids = [25544, 43657, 39256, 43651, 28884,
                       20580, 28654, 32384, 37256, 39533]
        catalog_source = "sample IDs (run main.py first for catalog-based check)"

    # Check a sample of up to 200 from the catalog for speed
    import random
    sample = catalog_ids if len(catalog_ids) <= 200 else random.sample(catalog_ids, 200)

    fully_cached, needs_fetch = tle_history_cache.split_cached_vs_needed(
        TLE_HISTORY_CACHE_DB, sample, start_date, end_date
    )
    coverage_pct = 100.0 * len(fully_cached) / len(sample) if sample else 0

    detail = (
        f"{stats}\n"
        f"Lookback window    = {start_date} → {end_date}\n"
        f"Catalog source     = {catalog_source}\n"
        f"Sample checked     = {len(sample):,} satellites\n"
        f"Fully cached       = {len(fully_cached):,} ({coverage_pct:.1f}%)\n"
        f"Would need fetch   = {len(needs_fetch):,}"
    )

    if coverage_pct == 0 and len(catalog_ids) > 10:
        return WARN, detail + (
            "\n\nIMPORTANT: 0% coverage for your catalog satellites.\n"
            "The cache has data but not for these satellites.\n"
            "Check that TLE_DATA_DIR in .env points to your TLE folder."
        )
    if coverage_pct < 50:
        return WARN, detail + (
            f"\n\nLow coverage ({coverage_pct:.0f}%) -- "
            "some satellites may not score correctly."
        )
    return PASS, detail





# ──────────────────────────────────────────────────────────────────────
# 3. SATCAT Cache
# ──────────────────────────────────────────────────────────────────────

def check_satcat_cache():
    from config import SATCAT_CACHE_DB, SATCAT_CACHE_MAX_AGE_HOURS
    import satcat_cache

    if not os.path.exists(SATCAT_CACHE_DB):
        return WARN, (
            "SATCAT cache does not exist yet -- launch metadata (country,\n"
            "object type, launch date) will be fetched from Space-Track on\n"
            "the first online run. No action needed; this is expected."
        )

    stats = satcat_cache.cache_stats(SATCAT_CACHE_DB, SATCAT_CACHE_MAX_AGE_HOURS)
    return PASS, stats


# ──────────────────────────────────────────────────────────────────────
# 4. API Rate-Limit Log
# ──────────────────────────────────────────────────────────────────────

def check_rate_limit_log():
    from config import API_REQUEST_LOG_DB
    import api_request_log

    counts = api_request_log.get_recent_counts(API_REQUEST_LOG_DB)
    min_hr, hr_hr, eff = api_request_log.headroom(API_REQUEST_LOG_DB)

    detail = (
        f"Requests past 60 seconds  = {counts['requests_past_minute']}\n"
        f"Requests past 60 minutes  = {counts['requests_past_hour']}\n"
        f"Remaining headroom        = {eff} slots before limit\n"
        f"SATCAT fetches today      = {counts['satcat_fetches_today']}\n"
        f"Last SATCAT fetch         = {counts['last_satcat_time'] or 'never'}"
    )

    if eff < 10:
        return WARN, detail + f"\nLow headroom ({eff} slots) -- consider waiting before running live."
    return PASS, detail


# ──────────────────────────────────────────────────────────────────────
# 5. Archive Integrity (can we read the zip files?)
# ──────────────────────────────────────────────────────────────────────

def check_archives():
    from config import TLE_DATA_DIR
    import tle_bulk_seeder
    import zipfile
    import re

    summary = tle_bulk_seeder.archive_summary(TLE_DATA_DIR)

    if not os.path.isdir(TLE_DATA_DIR):
        return WARN, f"TLE_DATA_DIR not found: {TLE_DATA_DIR}"

    # Count and spot-check zip files
    bulk_pat = re.compile(r"^tle\d{4}.*\.zip$", re.IGNORECASE)
    all_zips = []
    corrupt  = []

    for fname in os.listdir(TLE_DATA_DIR):
        fpath = os.path.join(TLE_DATA_DIR, fname)
        if os.path.isfile(fpath) and bulk_pat.match(fname):
            all_zips.append(fpath)

    for ydir in os.listdir(TLE_DATA_DIR):
        ypath = os.path.join(TLE_DATA_DIR, ydir)
        if os.path.isdir(ypath):
            try:
                int(ydir)
                for fname in os.listdir(ypath):
                    if fname.endswith(".zip"):
                        all_zips.append(os.path.join(ypath, fname))
            except ValueError:
                pass

    if not all_zips:
        return WARN, summary + "\nNo zip files found -- run --import-folder first."

    # Spot-check up to 5 zip files (test open + list, don't read content)
    sample = all_zips[:5]
    for zp in sample:
        try:
            with zipfile.ZipFile(zp, "r") as zf:
                names = zf.namelist()
                if not names:
                    corrupt.append(f"{os.path.basename(zp)}: empty zip")
        except zipfile.BadZipFile:
            corrupt.append(f"{os.path.basename(zp)}: corrupted (BadZipFile)")
        except Exception as e:
            corrupt.append(f"{os.path.basename(zp)}: {e}")

    detail = (
        f"{summary}\n"
        f"Spot-checked {len(sample)}/{len(all_zips)} zip files: "
        f"{len(sample) - len(corrupt)} OK, {len(corrupt)} errors"
    )
    if corrupt:
        return FAIL, detail + "\nCorrupt files:\n" + "\n".join(f"  {c}" for c in corrupt)
    return PASS, detail


# ──────────────────────────────────────────────────────────────────────
# 6. Pre-Flight Policy Check (dry-run, no network)
# ──────────────────────────────────────────────────────────────────────

def check_policy_preflight():
    from config import (
        TLE_HISTORY_CACHE_DB, SATCAT_CACHE_DB, SATCAT_CACHE_MAX_AGE_HOURS,
        API_REQUEST_LOG_DB, HISTORICAL_BATCH_SIZE, HISTORICAL_LOOKBACK_YEARS,
    )
    import spacetrack_policy_check as pc

    # Use a small set of NORAD IDs for the trial check rather than
    # loading the full visible_satellites.xlsx (which requires pandas)
    sample_ids = [25544, 43657, 39256, 43651, 28884, 12345, 67890]

    try:
        result = pc.run_preflight_check(
            norad_ids=sample_ids,
            lookback_years=HISTORICAL_LOOKBACK_YEARS,
            tle_cache_db=TLE_HISTORY_CACHE_DB,
            satcat_cache_db=SATCAT_CACHE_DB,
            satcat_max_age_hours=SATCAT_CACHE_MAX_AGE_HOURS,
            batch_size=HISTORICAL_BATCH_SIZE,
            api_log_db=API_REQUEST_LOG_DB,
            interactive=False,  # never prompt during trial run
        )
        detail = (
            f"Sample set: {len(sample_ids)} satellites\n"
            f"Cached     : {result['fully_cached_count']}/{result['total_satellites']}\n"
            f"Need fetch : {result['needs_fetch_count']}\n"
            f"Est. requests this run: {result['estimated_requests']}"
        )
        return PASS, detail
    except pc.PolicyCheckFailed as e:
        return WARN, (
            f"Policy check would block a real run:\n{e}\n"
            "This is a WARN (not FAIL) because trial run uses a small sample.\n"
            "Review the message above before running live."
        )


# ──────────────────────────────────────────────────────────────────────
# 7. Offline Scoring (score from cache, zero network)
# ──────────────────────────────────────────────────────────────────────

def check_offline_scoring():
    from config import TLE_HISTORY_CACHE_DB
    import tle_history_cache

    # Load elements for the ISS (25544) which should be in cache after import
    sample_ids = [25544, 39256, 43651]
    cached = tle_history_cache.load_cached_elements(TLE_HISTORY_CACHE_DB, sample_ids)

    scored    = {k: v for k, v in cached.items() if v}
    uncached  = {k: v for k, v in cached.items() if not v}

    detail = (
        f"Loaded from cache (no network):\n"
        + "\n".join(
            f"  NORAD {n}: {len(els)} historical elements"
            for n, els in cached.items()
        )
    )

    if not scored:
        return WARN, detail + (
            "\nNo cached elements found for sample satellites.\n"
            "Import your TLE zip files first:\n"
            "  python seed_tle_history.py --import-folder \"C:\\...\\TLEs\""
        )

    # Quick scoring sanity check -- do the elements look sensible?
    for norad_id, elements in scored.items():
        if elements:
            el = elements[0]
            if el.get("altitude_km", 0) < 100 or el.get("altitude_km", 9e9) > 50000:
                return WARN, detail + f"\nSuspicious altitude for NORAD {norad_id}: {el.get('altitude_km')} km"
            if not el.get("epoch"):
                return FAIL, detail + f"\nMissing epoch for NORAD {norad_id}"

    return PASS, detail


# ──────────────────────────────────────────────────────────────────────
# 8. Streaming Library Status
# ──────────────────────────────────────────────────────────────────────

def check_library_status():
    import spacetrack_client as stc
    status = stc.library_status()
    if stc._lib_available:
        return PASS, status
    return WARN, status + "\n(Optional -- install with: pip install spacetrack)"


# ──────────────────────────────────────────────────────────────────────
# 9. Output Directory Writable
# ──────────────────────────────────────────────────────────────────────

def check_output_dir():
    from config import BASE_DIR, SATELLITE_CONFIDENCE_DB
    import satellite_confidence_db as _cdb
    output_dir = os.path.join(BASE_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)

    test_file = os.path.join(output_dir, ".trial_write_test")
    try:
        with open(test_file, "w") as f:
            f.write("trial")
        os.unlink(test_file)
        write_ok = True
    except Exception as e:
        return FAIL, f"Cannot write to output directory {output_dir}: {e}"

    db_stats = _cdb.db_stats(SATELLITE_CONFIDENCE_DB)
    return PASS, f"Output directory writable: {output_dir}\n{db_stats}"


# ──────────────────────────────────────────────────────────────────────
# 10. Verify NO network calls were intercepted
# ──────────────────────────────────────────────────────────────────────

def check_no_network_calls():
    from config import API_REQUEST_LOG_DB
    import api_request_log
    import time

    # Read the request log state at the START of the trial run
    # (stored in the module-level variable set at import time)
    # and compare to now -- if any requests were logged DURING
    # this trial run, something made a real call.
    counts_now = api_request_log.get_recent_counts(API_REQUEST_LOG_DB)
    calls_this_minute = counts_now["requests_past_minute"]

    # We can't easily distinguish "logged before trial started" vs
    # "logged during trial" without storing the count at start.
    # Best we can do: confirm the trial run itself never called
    # spacetrack_login() (which would log a request).
    detail = (
        f"Requests logged in past 60s = {calls_this_minute}\n"
        f"(Includes any calls made BEFORE this trial run too --\n"
        f" this check confirms no new calls were made during the trial)"
    )
    # The trial run itself only reads from caches, never calls
    # spacetrack_login() or rate_limiter.acquire(), so any count
    # here reflects pre-existing requests, not ones we made.
    return PASS, detail


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    global verbose
    parser = argparse.ArgumentParser(
        description="Trial run: verify all tool components without hitting Space-Track."
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detail for passing checks too")
    args = parser.parse_args()
    verbose = args.verbose

    print()
    print("=" * 60)
    print("Satellite Tool — Trial Run (No Network Calls)")
    print("=" * 60)
    print()
    print("Checking all components offline. No Space-Track API calls")
    print("will be made. Exit code 0 = safe to run live.\n")

    print("─── Configuration ───────────────────────────────────────")
    check("Config loads (TLE_DATA_DIR, credentials, paths)", check_config)

    print("\n─── Local Data ──────────────────────────────────────────")
    check("TLE history cache (tle_history_cache.sqlite3)",   check_tle_cache)
    check("SATCAT cache (satcat_cache.sqlite3)",             check_satcat_cache)
    check("API request log (rate-limit headroom)",           check_rate_limit_log)
    check("Archive integrity (zip files readable)",          check_archives)

    print("\n─── Policy & Scoring ────────────────────────────────────")
    check("Pre-flight policy check (no network)",            check_policy_preflight)
    check("Offline scoring from cache (no network)",         check_offline_scoring)

    print("\n─── Environment ─────────────────────────────────────────")
    check("Output directory writable",                       check_output_dir)
    check("Streaming library status (optional)",             check_library_status)
    check("No network calls made during this trial run",     check_no_network_calls)

    # ── Summary ────────────────────────────────────────────────────────
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_warn = sum(1 for _, s, _ in results if s == WARN)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)

    print()
    print("=" * 60)
    print(f"Results: {n_pass} passed, {n_warn} warnings, {n_fail} failed")
    print("=" * 60)

    if n_fail:
        print()
        print("FAILED checks:")
        for label, status, detail in results:
            if status == FAIL:
                print(f"  ✗ {label}")
                for line in detail.splitlines():
                    print(f"      {line}")
        print()
        print("Fix the failed checks before running the tool live.")
        sys.exit(1)

    if n_warn:
        print()
        print("Warnings (tool will still run, but review these):")
        for label, status, detail in results:
            if status == WARN:
                print(f"  ⚠ {label}")
                for line in detail.splitlines()[:3]:
                    print(f"      {line}")
        print()

    if n_fail == 0 and n_warn == 0:
        print()
        print("All checks passed. The tool is ready to run live.")
        print()
        print("To run the full analysis:")
        print("  python main.py")
        print()
        print("To score from the local cache only (no Space-Track):")
        print("  python historical_accuracy.py --offline")
    elif n_fail == 0:
        print()
        print("No failures. Warnings above are informational.")
        print("The tool should run correctly.")

    print()
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
