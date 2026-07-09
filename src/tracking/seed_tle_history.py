"""
seed_tle_history.py — TLE history management utility

USAGE
======

IMPORT FROM ALREADY-DOWNLOADED FILES (fastest -- no credentials needed):
If you've already downloaded the Space-Track publicfiles bulk zips to a
local folder (e.g. from the Sync.com link), import them directly:
    python seed_tle_history.py --import-folder "C:\\Users\\toddl\\OneDrive\\Data Science Project\\Data\\TLEs"

To import only the years you need (3yr lookback = last 3 years):
    python seed_tle_history.py --import-folder "C:\\...\\TLEs" --start-year 2023

After import, pull any new records published since the files were downloaded:
    python seed_tle_history.py --delta

If publicfiles aren't available (they're account-permission-dependent),
fall back to the date-range gp_history approach instead:
    python seed_tle_history.py --seed

ONGOING MAINTENANCE: After the initial seed, use incremental delta sync
to pull only what's changed since the last run (one gp_history request
using the FILE predicate, as documented in Space-Track's How-To):
    python seed_tle_history.py --delta

DAILY SNAPSHOT (schedule via Task Scheduler):
Uses class/gp (NOT gp_history) -- policy 1/hour, completely separate:
    python seed_tle_history.py --daily

REBUILD CACHE FROM LOCAL ZIPS (no network, no credentials):
    python seed_tle_history.py --reimport

SHOW STATUS:
    python seed_tle_history.py --status

How many years to seed (default: 3, only used with --seed):
    python seed_tle_history.py --seed --years 5
"""

import sys
import os
import argparse
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    SPACETRACK_USERNAME,
    SPACETRACK_PASSWORD,
    TLE_HISTORY_CACHE_DB,
    API_REQUEST_LOG_DB,
    BASE_DIR,
    TLE_DATA_DIR,
)
from satellite_utils import spacetrack_login, SpaceTrackRateLimiter
import tle_bulk_seeder
import api_request_log


def _make_rate_limiter():
    return SpaceTrackRateLimiter(
        log_callback=lambda cls, n: api_request_log.log_request(
            API_REQUEST_LOG_DB, cls, norad_count=n
        )
    )


def _login():
    if not SPACETRACK_USERNAME or not SPACETRACK_PASSWORD:
        print("ERROR: SPACETRACK_USERNAME / SPACETRACK_PASSWORD not set in .env")
        sys.exit(1)
    print("Logging in to Space-Track...")
    session = spacetrack_login(SPACETRACK_USERNAME, SPACETRACK_PASSWORD)
    api_request_log.log_request(API_REQUEST_LOG_DB, api_request_log.CLASS_LOGIN)
    return session


def cmd_import_folder(folder_path, start_year=None, end_year=None):
    """Import from locally-downloaded Space-Track publicfiles zip files."""
    print("=" * 60)
    print("TLE History Import -- Local Folder")
    print("=" * 60)
    print(
        f"Source folder: {folder_path}\n"
        f"Destination cache: {TLE_HISTORY_CACHE_DB}\n"
    )

    if not os.path.isdir(folder_path):
        print(f"ERROR: Folder not found: {folder_path}")
        print("Please check the path and try again.")
        sys.exit(1)

    # Show what's in the folder before starting
    import re
    zips = [f for f in os.listdir(folder_path)
            if f.lower().endswith(".zip") and f.lower().startswith("tle")]
    zips.sort()
    print(f"Found {len(zips)} zip file(s) in folder:")
    for z in zips[:5]:
        print(f"  {z}")
    if len(zips) > 5:
        print(f"  ... and {len(zips)-5} more")
    print()

    if start_year or end_year:
        y0 = start_year or "earliest"
        y1 = end_year or "latest"
        print(f"Year filter: {y0} – {y1}")
        print()
    else:
        print(
            "No year filter set. All years will be imported.\n"
            "For a 3-year confidence lookback, you only need the most\n"
            f"recent 3 years. Use --start-year {date.today().year - 3} "
            f"to limit the import.\n"
        )

    total_size_mb = sum(
        os.path.getsize(os.path.join(folder_path, z)) / (1024*1024)
        for z in zips
    )
    print(f"Total size to process: {total_size_mb:.0f} MB")
    print()

    confirm = input("Proceed with import? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    result = tle_bulk_seeder.import_from_local_folder(
        folder_path=folder_path,
        cache_db_path=TLE_HISTORY_CACHE_DB,
        tle_data_dir=TLE_DATA_DIR,
        start_year=start_year,
        end_year=end_year,
    )

    print()
    import tle_history_cache
    print(tle_history_cache.cache_stats(TLE_HISTORY_CACHE_DB))
    print()
    print(
        "Import complete. The tool will now use the local cache for\n"
        "confidence scoring with no Space-Track gp_history queries.\n"
        "\n"
        "Run 'python seed_tle_history.py --delta' to pull any TLE\n"
        "records published since these files were downloaded."
    )
    """Download pre-packaged yearly TLE zip files from Space-Track's publicfiles."""
    print("=" * 60)
    print("TLE History Seed -- publicfiles (cloud storage)")
    print("=" * 60)
    print(
        "Downloading pre-packaged yearly TLE zips from Space-Track's\n"
        "cloud storage site (the approach documented in gp_history:\n"
        "'download TLEs bundled as zip files by year from our cloud\n"
        " storage site instead').\n"
        "\n"
        "Files Panel policy: 1/lifetime per file -- download once,\n"
        "store locally, never download again.\n"
    )
    print(tle_bulk_seeder.archive_summary(TLE_DATA_DIR))
    print()

    session = _login()
    rate_limiter = _make_rate_limiter()

    result = tle_bulk_seeder.seed_from_publicfiles(
        session=session,
        cache_db_path=TLE_HISTORY_CACHE_DB,
        tle_data_dir=TLE_DATA_DIR,
        start_year=start_year,
        end_year=end_year,
        rate_limiter=rate_limiter,
        request_log_db=API_REQUEST_LOG_DB,
    )
    session.close()

    if result.get("fallback_needed"):
        print(
            "\nPublicfiles unavailable. You can use --seed instead for a\n"
            "date-range gp_history pull as the fallback."
        )
    else:
        print(f"\n{tle_bulk_seeder.archive_summary(TLE_DATA_DIR)}")
        print("\nSetup complete. Run --delta for ongoing incremental updates.")


def cmd_delta():
    """Incremental delta sync -- pull only new records since last sync."""
    print("=" * 60)
    print("TLE History -- Incremental Delta Sync")
    print("=" * 60)
    print(
        "Fetching only TLE records published after the last known\n"
        "FILE number (Space-Track's How-To 'download just the changes'\n"
        "method). One gp_history request, pulls only what's new.\n"
    )

    session = _login()
    rate_limiter = _make_rate_limiter()

    records, new_bookmark = tle_bulk_seeder.sync_incremental_delta(
        session=session,
        cache_db_path=TLE_HISTORY_CACHE_DB,
        tle_data_dir=TLE_DATA_DIR,
        rate_limiter=rate_limiter,
        request_log_db=API_REQUEST_LOG_DB,
    )
    session.close()
    print(f"\n{tle_bulk_seeder.archive_summary(TLE_DATA_DIR)}")


def cmd_seed(years, resume=True):
    """Fallback: date-range gp_history bulk seed by calendar month."""
    print("=" * 60)
    print("TLE History Bulk Seed (date-range gp_history fallback)")
    print("=" * 60)
    print(
        f"Fetching {years} year(s) of gp_history via monthly date-range\n"
        f"queries. Use --publicfiles first if available -- it's faster.\n"
    )
    print(tle_bulk_seeder.archive_summary(TLE_DATA_DIR))
    print()

    if not SPACETRACK_USERNAME or not SPACETRACK_PASSWORD:
        print("ERROR: SPACETRACK_USERNAME / SPACETRACK_PASSWORD not set in .env")
        sys.exit(1)

    end = date.today()
    start = date(end.year - years, end.month, end.day)

    archive_base = TLE_DATA_DIR
    slices_needed = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        zp = tle_bulk_seeder.monthly_zip_path(archive_base, y, m)
        if not os.path.exists(zp):
            slices_needed.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1

    if not slices_needed:
        print("All months already archived. Nothing to fetch.")
        return

    print(f"{len(slices_needed)} month(s) to fetch ({len(slices_needed)} requests).\n")
    confirm = input("Proceed? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    session = _login()
    rate_limiter = _make_rate_limiter()

    tle_bulk_seeder.bulk_seed_from_gp_history(
        session=session,
        cache_db_path=TLE_HISTORY_CACHE_DB,
        tle_data_dir=archive_base,
        start_year=start.year,
        start_month=start.month,
        end_year=end.year,
        end_month=end.month,
        rate_limiter=rate_limiter,
        timeout_sec=300,
        request_log_db=API_REQUEST_LOG_DB,
    )
    session.close()
    print(f"\n{tle_bulk_seeder.archive_summary(archive_base)}")


def cmd_daily():
    """Daily GP snapshot (class/gp, not gp_history)."""
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    archive_base = TLE_DATA_DIR
    print(f"Daily GP snapshot for {today}")

    zp = tle_bulk_seeder.daily_zip_path(archive_base, today)
    if os.path.exists(zp):
        print(f"Today's snapshot already exists. Nothing to do.")
        return

    session = _login()
    rate_limiter = _make_rate_limiter()
    records, skipped = tle_bulk_seeder.snapshot_daily_gp(
        session=session, cache_db_path=TLE_HISTORY_CACHE_DB,
        tle_data_dir=archive_base, snapshot_date=today,
        rate_limiter=rate_limiter, request_log_db=API_REQUEST_LOG_DB,
    )
    session.close()


def cmd_reimport():
    """Rebuild SQLite cache from local zip archives."""
    archive_base = TLE_DATA_DIR
    print("=" * 60)
    print("Rebuilding TLE history cache from local archives")
    print("=" * 60)
    print(tle_bulk_seeder.archive_summary(archive_base))
    confirm = input("\nThis will overwrite the current cache. Proceed? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return
    total = tle_bulk_seeder.import_from_zips(TLE_HISTORY_CACHE_DB, archive_base)
    print(f"\nReimport complete: {total:,} records.")


def cmd_rollup(year=None, dry_run=False):
    """Roll up daily/monthly zip files into compact year-packet zips."""
    print("=" * 60)
    print(f"TLE Archive Rollup {'(DRY RUN) ' if dry_run else ''}-- Year Packets")
    print("=" * 60)
    print(
        f"Data directory: {TLE_DATA_DIR}\n"
        f"\n"
        f"Merges individual daily/monthly zip files into single\n"
        f"year-packet zips (YYYY/YYYY.zip) to reduce file count\n"
        f"and reclaim disk space. Individual files are removed\n"
        f"after being safely merged into the year zip.\n"
    )

    from datetime import date
    current_year = date.today().year

    if year:
        if year >= current_year and not dry_run:
            response = input(
                f"  {year} is the current year -- new daily files will\n"
                f"  continue to be added to it after the rollup. Proceed? (yes/no): "
            ).strip().lower()
            if response != "yes":
                print("Cancelled.")
                return
        tle_bulk_seeder.rollup_year_to_zip(TLE_DATA_DIR, year, dry_run=dry_run)
    else:
        print(f"Rolling up all complete years before {current_year}...\n")
        results = tle_bulk_seeder.rollup_all_complete_years(TLE_DATA_DIR, dry_run=dry_run)
        if not results:
            print("  No complete years with unrolled daily files found.")
            return
        total_freed = sum(r.get("space_freed_mb", 0) for _, r in results)
        total_merged = sum(r.get("merged_files", 0) for _, r in results)
        if not dry_run:
            print(
                f"\nRollup complete: {total_merged} files merged across "
                f"{len(results)} year(s), {total_freed:.1f} MB freed."
            )

    if not dry_run:
        print()
        print(tle_bulk_seeder.archive_summary(TLE_DATA_DIR))


def cmd_status():
    """Show archive, cache, and library status."""
    import tle_history_cache
    import spacetrack_client as stc
    print("=" * 60)
    print("TLE Archive and Cache Status")
    print("=" * 60)
    print(f"Data directory: {TLE_DATA_DIR}\n")
    print(tle_bulk_seeder.archive_summary(TLE_DATA_DIR))
    print()
    print(tle_history_cache.cache_stats(TLE_HISTORY_CACHE_DB))
    print()
    print(stc.library_status())
    print()
    bookmark = tle_bulk_seeder._get_bookmark(TLE_HISTORY_CACHE_DB)
    if bookmark:
        print(f"Incremental sync bookmark: FILE > {bookmark}")
    else:
        print("Incremental sync bookmark: not set (run --import-folder or --seed first)")
    print()
    counts = api_request_log.get_recent_counts(API_REQUEST_LOG_DB)
    _, _, eff = api_request_log.headroom(API_REQUEST_LOG_DB)
    print(
        f"Rate-limit status (cross-run):\n"
        f"  Past 60 seconds: {counts['requests_past_minute']} requests\n"
        f"  Past 60 minutes: {counts['requests_past_hour']} requests\n"
        f"  Remaining headroom: {eff} slots\n"
        f"  SATCAT fetches today: {counts['satcat_fetches_today']}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="TLE history management: import, delta sync, daily snapshots, year rollup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--import-folder", metavar="PATH",
                       help="Import from a local folder of downloaded Space-Track zip files "
                            "(e.g. C:\\Users\\...\\Data\\TLEs) -- no credentials needed")
    group.add_argument("--publicfiles", action="store_true",
                       help="Download bulk yearly TLE zips from Space-Track's cloud storage")
    group.add_argument("--delta",       action="store_true",
                       help="Incremental delta sync: pull only records newer than last FILE bookmark")
    group.add_argument("--seed",        action="store_true",
                       help="Fallback: date-range gp_history monthly queries")
    group.add_argument("--daily",       action="store_true",
                       help="Daily GP snapshot via class/gp (schedule via Task Scheduler)")
    group.add_argument("--rollup",      action="store_true",
                       help="Roll up daily zip files into year-packet zips to save space "
                            "(use --year YYYY to roll up a specific year, "
                            "or omit for all complete years)")
    group.add_argument("--rebuild-coverage", action="store_true",
                       help="Rebuild the coverage index from tle_elements (run if coverage shows 0 satellites after import)")
    group.add_argument("--reimport",    action="store_true",
                       help="Rebuild SQLite cache from local zip archives (no network needed)")
    group.add_argument("--status",      action="store_true",
                       help="Show archive, cache, and rate-limit status")
    parser.add_argument("--year",       type=int, default=None,
                        help="Year to target with --rollup (default: all complete years)")
    parser.add_argument("--dry-run",    action="store_true",
                        help="With --rollup: show what would be done without changing files")
    parser.add_argument("--years",      type=int, default=3,
                        help="Years of history to seed with --seed (default: 3)")
    parser.add_argument("--start-year", type=int, default=None,
                        help="First year to include with --import-folder or --publicfiles")
    parser.add_argument("--end-year",   type=int, default=None,
                        help="Last year to include with --import-folder or --publicfiles")

    args = parser.parse_args()

    if args.import_folder:
        cmd_import_folder(args.import_folder, args.start_year, args.end_year)
    elif args.publicfiles:
        cmd_publicfiles(args.start_year, args.end_year)
    elif args.delta:
        cmd_delta()
    elif args.seed:
        cmd_seed(args.years)
    elif args.daily:
        cmd_daily()
    elif args.rollup:
        cmd_rollup(year=args.year, dry_run=args.dry_run)
    elif args.rebuild_coverage:
        import tle_history_cache
        print("=" * 60)
        print("Rebuilding TLE history cache coverage index")
        print("=" * 60)
        n = tle_history_cache.rebuild_coverage_from_elements(TLE_HISTORY_CACHE_DB)
        print(f"\n{tle_history_cache.cache_stats(TLE_HISTORY_CACHE_DB)}")
    elif args.reimport:
        cmd_reimport()
    elif args.status:
        cmd_status()


if __name__ == "__main__":
    main()
