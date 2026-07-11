"""
TLE Fetcher — CelesTrak
========================
Fetches GP (General Perturbations) orbital data for satellites and
writes them to the database via src/db/writer.py.

Usage:
    python src/tle/fetcher.py                  # fetch all groups, write to DB
    python src/tle/fetcher.py --group starlink  # fetch one group
    python src/tle/fetcher.py --dry-run         # fetch and parse, no DB write
    python src/tle/fetcher.py --list-groups     # list available groups and exit

Environment:
    DATABASE_URL=postgresql://...   (required unless --dry-run)

─────────────────────────────────────────────────────────────────────────
REWRITE NOTES (2026-07-10)
─────────────────────────────────────────────────────────────────────────
This replaces the previous version, which was non-functional. Two
separate problems, both fixed here:

1. DEAD URLS. The previous PRIMARY_URLS dict (the one fetch_all()
   actually used -- CELESTRAK_GROUPS and CELESTRAK_TLE_URLS were defined
   but never referenced) pointed at celestrak.org/pub/TLE/*.txt static
   files. CelesTrak permanently removed ALL legacy static .txt files on
   2024-12-24 (see celestrak.org's own current-data page notice, and
   https://celestrak.org/NORAD/documentation/gp-data-formats.php) to push
   users toward the dynamic gp.php query endpoint. Those URLs 404 now --
   this tool could not have been fetching real data. The separate
   CELESTRAK_GROUPS dict pointed at celestrak.org/SOCRATES/query.php,
   which is CelesTrak's conjunction-assessment tool, not the GP catalog,
   and was never wired up regardless.

   Fixed: all groups now use the current, documented endpoint --
   https://celestrak.org/NORAD/elements/gp.php?GROUP=<name>&FORMAT=<fmt>

2. 5-DIGIT NORAD ID CEILING. CelesTrak's own catalog notice: they run
   out of 5-digit catalog numbers at 69999 (not 99999), estimated around
   2026-07-12, after which new objects get 6-digit IDs and simply aren't
   representable in the fixed-width TLE format at all.

   Fixed: switched FORMAT=tle -> FORMAT=json and dropped the fixed-width
   line-slicing parser (parse_norad_id, parse_epoch, TLERecord.inclination
   /.mean_motion via line[8:16] etc.) in favor of reading OMM JSON fields
   directly (NORAD_CAT_ID, MEAN_MOTION, INCLINATION, ...) -- no digit
   limit, no column-offset fragility.

   Classic TLE line1/line2 strings are still generated per satellite (the
   satellites.tle_line1/tle_line2 schema columns expect them) via sgp4's
   own exporter, which supports Alpha-5 encoding for catalog numbers up
   to ~339999 -- comfortably past the 2026-07-12 transition. Generation
   is wrapped in a try/except and left NULL (with a warning) for any
   object outside Alpha-5's range rather than failing the whole fetch.

This module now also does what its docstring always said it would
("Phase 1+" DB write) but never implemented: it calls src/db/writer.py's
upsert_satellites() / insert_tle_history() and logs the run via
new_run_id() / log_step(), instead of just printing a record count.
"""

import os
import sys
import time
import logging
import argparse
import json as _json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ── Make src/db/writer.py importable regardless of CWD ─────────────────────
# fetcher.py lives in src/tle/; writer.py lives in the sibling src/db/.
# Inserting that directory directly (rather than requiring a package
# structure / PYTHONPATH setup) matches how the rest of this repo's
# scripts import their neighbors (e.g. src/tracking/*.py's bare
# `from config import ...`), so `python src/tle/fetcher.py` works from
# any working directory without extra setup.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_SRC_DIR, "db"))

try:
    from writer import (
        upsert_satellites,
        insert_tle_history,
        new_run_id,
        log_step,
        check_connection,
    )
    _WRITER_AVAILABLE = True
except Exception as _writer_import_error:  # pragma: no cover
    _WRITER_AVAILABLE = False
    _WRITER_IMPORT_ERROR = _writer_import_error

try:
    from sgp4.api import Satrec
    from sgp4 import exporter as _sgp4_exporter
    from sgp4 import omm as _sgp4_omm
    _SGP4_AVAILABLE = True
except ImportError:
    _SGP4_AVAILABLE = False


# ── Configuration ─────────────────────────────────────────────────────────────

# Current, documented CelesTrak GP endpoint. GROUP names below match
# CelesTrak's published group list (celestrak.org/NORAD/elements/).
_GP_BASE_URL = "https://celestrak.org/NORAD/elements/gp.php"

CELESTRAK_GROUPS = [
    "active", "stations", "visual", "weather", "noaa", "goes",
    "resource", "starlink", "oneweb", "gps-ops", "glo-ops", "galileo",
    "beidou", "geo", "debris",
]

# Same throttle notice CelesTrak returns if a group is requested again
# before its 2-hour update window has passed (see src/tle/gp_json.py,
# which shares this exact pattern for the src/tracking/ tool's fetch).
_CELESTRAK_THROTTLE_MARKER = "has not updated since your last successful"

REQUEST_TIMEOUT = 30   # seconds
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5    # seconds between retries


def _group_url(group: str, fmt: str = "json") -> str:
    return f"{_GP_BASE_URL}?GROUP={group}&FORMAT={fmt}"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TLERecord:
    """One satellite's GP data, as read from CelesTrak's OMM JSON."""
    name:            str
    norad_id:        int
    intl_designator: str
    epoch:           str
    mean_motion:     float
    eccentricity:    float
    inclination:     float
    regime:          str
    group:           str
    tle_line1:       Optional[str] = None
    tle_line2:       Optional[str] = None
    fetched_at:      datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "norad_id": self.norad_id,
            "intl_designator": self.intl_designator,
            "epoch": self.epoch,
            "regime": self.regime,
            "group": self.group,
            "inclination": self.inclination,
            "mean_motion": self.mean_motion,
            "eccentricity": self.eccentricity,
            "tle_line1": self.tle_line1,
            "tle_line2": self.tle_line2,
            "fetched_at": self.fetched_at.isoformat(),
        }


# ── Regime classifier ─────────────────────────────────────────────────────────

def classify_regime(mean_motion: float, inclination: float) -> str:
    """
    Classify orbital regime from mean motion (rev/day) and inclination.

    Mean motion -> approximate altitude:
      > 11.25   -> LEO  (< ~2000 km)
      2.0-11.25 -> MEO  (2000-35786 km)
      ~1.0      -> GEO  (~35786 km, geostationary)
      < 1.0     -> HEO  (highly elliptical)
    """
    if mean_motion > 11.25:
        return "LEO"
    elif mean_motion >= 2.0:
        return "MEO"
    elif 0.9 <= mean_motion <= 1.1:
        return "GEO"
    else:
        return "HEO"


# ── TLE line export (best-effort, for display / the schema's tle_line1/2) ──

def _export_tle_lines(omm_fields: dict) -> tuple:
    """
    Best-effort classic TLE line1/line2 for storage/display, generated
    from the OMM record via sgp4's exporter (which supports Alpha-5
    encoding for catalog numbers up to ~339999 -- past the 2026-07-12
    5-digit transition). Returns (None, None) if sgp4 isn't available or
    the object's catalog number is outside what any TLE encoding (even
    Alpha-5) can represent -- this is expected for 9-digit SDS launch-
    nominal numbers and is not treated as an error.
    """
    if not _SGP4_AVAILABLE:
        return None, None
    try:
        satrec = Satrec()
        _sgp4_omm.initialize(satrec, omm_fields)
        line1, line2 = _sgp4_exporter.export_tle(satrec)
        return line1, line2
    except Exception as exc:
        log.debug(
            "Could not export TLE lines for NORAD %s (expected for "
            "catalog numbers beyond Alpha-5 range): %s",
            omm_fields.get("NORAD_CAT_ID"), exc,
        )
        return None, None


# ── JSON/OMM parser ──────────────────────────────────────────────────────────

def parse_gp_json(records: list, group: str) -> list:
    """
    Parse a list of CelesTrak OMM JSON records (as returned by
    response.json()) into TLERecord objects.

    No digit-count limit and no fixed-column parsing -- unlike the old
    TLE-text parser, malformed/missing fields fail per-record (skipped,
    logged) rather than corrupting an entire 69-character line offset.
    """
    parsed = []
    skipped = 0

    for rec in records:
        try:
            norad_id = int(rec["NORAD_CAT_ID"])
            mean_motion = float(rec["MEAN_MOTION"])
            inclination = float(rec["INCLINATION"])
            eccentricity = float(rec.get("ECCENTRICITY", 0.0))

            tle_line1, tle_line2 = _export_tle_lines(rec)

            parsed.append(TLERecord(
                name=rec.get("OBJECT_NAME", "UNKNOWN"),
                norad_id=norad_id,
                intl_designator=rec.get("OBJECT_ID", ""),
                epoch=rec.get("EPOCH", ""),
                mean_motion=mean_motion,
                eccentricity=eccentricity,
                inclination=inclination,
                regime=classify_regime(mean_motion, inclination),
                group=group,
                tle_line1=tle_line1,
                tle_line2=tle_line2,
            ))
        except (KeyError, ValueError, TypeError) as exc:
            skipped += 1
            log.debug("Skipped malformed GP JSON record in group %s: %s", group, exc)

    if skipped:
        log.warning("  %s: skipped %d malformed record(s)", group, skipped)

    return parsed


# ── HTTP fetcher ──────────────────────────────────────────────────────────────

def fetch_url(url: str, session: requests.Session) -> Optional[str]:
    """Fetch a URL with retries. Returns text content or None."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as e:
            log.warning(f"Attempt {attempt}/{RETRY_ATTEMPTS} failed for {url}: {e}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY)
    log.error(f"All {RETRY_ATTEMPTS} attempts failed for {url}")
    return None


def fetch_group(group: str, session: requests.Session) -> list:
    """Fetch and parse one GP group as JSON."""
    log.info(f"  Fetching {group}...")
    url = _group_url(group, fmt="json")
    text = fetch_url(url, session)
    if not text:
        log.error(f"  Failed to fetch {group}")
        return []

    if _CELESTRAK_THROTTLE_MARKER in text:
        log.warning(
            f"  {group}: CelesTrak throttle notice (requested again "
            f"before the 2-hour update window) -- skipping this group "
            f"for this run."
        )
        return []

    try:
        records = _json.loads(text)
    except ValueError as exc:
        log.error(f"  {group}: response was not valid JSON: {exc}")
        return []

    parsed = parse_gp_json(records, group)
    log.info(f"  {group}: {len(parsed)} satellites")
    return parsed


# ── Main fetch function ───────────────────────────────────────────────────────

def fetch_all(groups: Optional[list] = None) -> list:
    """
    Fetch GP data from CelesTrak for specified groups (or all if None).
    Returns a deduplicated (by norad_id) list of TLERecord objects.
    """
    target_groups = groups or list(CELESTRAK_GROUPS)
    all_records = []
    seen_norad = set()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePlatform/1.0 (research project)"
    })

    print(f"\n{'=' * 55}")
    print(f"  GP Fetcher -- CelesTrak (JSON/OMM)")
    print(f"  Groups: {', '.join(target_groups)}")
    print(f"{'=' * 55}\n")

    for group in target_groups:
        if group not in CELESTRAK_GROUPS:
            log.warning(f"Unknown group: {group} -- skipping")
            continue

        records = fetch_group(group, session)

        new_records = []
        for r in records:
            if r.norad_id not in seen_norad:
                seen_norad.add(r.norad_id)
                new_records.append(r)

        all_records.extend(new_records)

        # Rate limiting -- be polite to CelesTrak.
        time.sleep(1)

    print(f"\n{'=' * 55}")
    print(f"  Total unique satellites: {len(all_records)}")

    regimes = {}
    for r in all_records:
        regimes[r.regime] = regimes.get(r.regime, 0) + 1
    for regime, count in sorted(regimes.items()):
        print(f"  {regime:>4}: {count:,}")
    print(f"{'=' * 55}\n")

    return all_records


# ── Database write ────────────────────────────────────────────────────────────

def write_records_to_db(records: list, run_id: str) -> int:
    """
    Upsert fetched records into satellites + tle_history via
    src/db/writer.py. Returns the number of satellites written.
    """
    if not _WRITER_AVAILABLE:
        raise RuntimeError(
            f"src/db/writer.py could not be imported: {_WRITER_IMPORT_ERROR}. "
            f"Make sure it exists at src/db/writer.py and DATABASE_URL is set, "
            f"or run with --dry-run to skip the database step."
        )

    satellite_rows = [
        {
            "norad_id": r.norad_id,
            "name": r.name,
            "intl_designator": r.intl_designator,
            "orbit_regime": r.regime,
            "tle_line1": r.tle_line1,
            "tle_line2": r.tle_line2,
            "tle_epoch": r.epoch,
            "mean_motion": r.mean_motion,
            "eccentricity": r.eccentricity,
            "source": "celestrak",
        }
        for r in records
    ]
    n = upsert_satellites(satellite_rows)

    # Only archive TLE history for records where we could actually
    # generate valid TLE lines (see _export_tle_lines -- None for
    # catalog numbers outside Alpha-5's range).
    tle_history_rows = [
        {
            "norad_id": r.norad_id,
            "line1": r.tle_line1,
            "line2": r.tle_line2,
            "epoch": r.epoch,
            "source": "celestrak",
        }
        for r in records
        if r.tle_line1 and r.tle_line2
    ]
    if tle_history_rows:
        insert_tle_history(tle_history_rows)

    log_step(
        run_id, pipeline="tle_fetch", step="write_db", status="success",
        records_processed=n, source="celestrak",
    )
    return n


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="GP data fetcher -- fetch satellite orbital data from CelesTrak"
    )
    parser.add_argument(
        "--group", type=str,
        help=f"Fetch one group only. Options: {', '.join(CELESTRAK_GROUPS)}"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and parse but do not write to the database"
    )
    parser.add_argument(
        "--list-groups", action="store_true",
        help="List available groups and exit"
    )
    args = parser.parse_args()

    if args.list_groups:
        print("\nAvailable CelesTrak groups:")
        for g in CELESTRAK_GROUPS:
            print(f"  {g:<12} {_group_url(g)}")
        return

    if not args.dry_run and _WRITER_AVAILABLE:
        if not check_connection():
            log.error(
                "Database connection check failed. Fix DATABASE_URL, or "
                "run with --dry-run to fetch without writing."
            )
            sys.exit(1)

    groups = [args.group] if args.group else None
    records = fetch_all(groups=groups)

    if args.dry_run:
        print("DRY RUN -- no database write. Showing first 5 records:")
        for r in records[:5]:
            print(f"  {r.norad_id:>6}  {r.name:<30}  {r.regime:<4}  "
                  f"epoch={r.epoch}")
        return records

    run_id = new_run_id()
    log_step(run_id, pipeline="tle_fetch", step="fetch", status="success",
              records_processed=len(records), source="celestrak")

    try:
        n = write_records_to_db(records, run_id)
        print(f"Wrote {n} satellites to database (run_id={run_id}).")
    except Exception as exc:
        log_step(run_id, pipeline="tle_fetch", step="write_db",
                  status="failed", message=str(exc), source="celestrak")
        raise

    return records


if __name__ == "__main__":
    main()
