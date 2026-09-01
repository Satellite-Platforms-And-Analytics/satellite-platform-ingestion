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
import time as _time
from pathlib import Path
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

# Verified against CelesTrak 2026-09-01 with --check-groups.
#
# DELIBERATELY MINIMAL - this is a courtesy-to-the-source decision, not an
# oversight. CelesTrak asks users not to re-request data they already have
# (celestrak.org/webmaster.php), and enforces it: an over-frequent request
# returns "GP data has not updated since your last successful..." and
# persistent abuse gets the client blocked.
#
# Measured on the 2026-09-01 run: 'active' returned 16,463 objects, and the
# twelve other configured groups downloaded a further 12,825 objects to
# contribute exactly 131 the catalogue did not already have. Twelve extra
# requests and ~13k objects of someone else's bandwidth for 131 rows.
#
# So: fetch 'active' for everything on orbit, then only the groups holding
# objects 'active' genuinely excludes. 5 requests per run instead of 17 -
# 60/day rather than 204 - while returning MORE unique objects than before.
#
# Adding a group here should be justified by objects it uniquely contains.
# --probe-groups lists 28 valid names; almost all are subsets of 'active'.
CELESTRAK_GROUPS = [
    "active",               # ~16,463  everything active and on orbit
    "analyst",              #     568  uncorrelated / analyst objects
    "cosmos-2251-debris",   #     584
    "iridium-33-debris",    #     111
    "cosmos-1408-debris",   #       3
]



# Same throttle notice CelesTrak returns if a group is requested again
# before its 2-hour update window has passed (see src/tle/gp_json.py,
# which shares this exact pattern for the src/tracking/ tool's fetch).
_CELESTRAK_THROTTLE_MARKER = "has not updated since your last successful"

REQUEST_TIMEOUT = 30   # seconds
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5    # seconds between retries
REQUEST_SPACING = 3    # seconds between groups -- courtesy to CelesTrak

#: CelesTrak regenerates GP data roughly every 2 hours; requesting a group
#: again inside that window returns a throttle notice rather than data.
#: This guard stops repeated LOCAL runs from hammering them - which is how
#: 65 requests went out in 10 minutes on 2026-09-01 while debugging.
MIN_REFETCH_SECONDS = 2 * 60 * 60


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


# ══════════════════════════════════════════════════════════════════════════════
#  Fetch log -- courtesy guard against re-requesting inside CelesTrak's window
# ══════════════════════════════════════════════════════════════════════════════
#
# src/tracking/ already does this properly for Space-Track (api_request_log.py,
# tle_history_cache.py, spacetrack_policy_check.py) because that account has
# been suspended before. The CelesTrak path had no equivalent: nothing stopped
# repeated manual runs from re-requesting the same groups minutes apart, which
# is exactly what happened on 2026-09-01 (80 requests, 65 within ten minutes).
#
# This records when each group was last fetched successfully and skips it if
# CelesTrak cannot have new data yet. --force overrides, for the rare case
# where a run genuinely needs to retry.

def _fetch_log_path() -> Path:
    base = os.environ.get("SATELLITE_DB_DIR", r"D:\Databases\satellite")
    return Path(base) / "celestrak_fetch_log.json"


def _load_fetch_log() -> dict:
    try:
        with open(_fetch_log_path(), "r", encoding="utf-8") as fh:
            return _json.load(fh)
    except Exception:
        return {}


def _record_fetch(group: str) -> None:
    path = _fetch_log_path()
    log_data = _load_fetch_log()
    log_data[group] = datetime.now(timezone.utc).isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(log_data, fh, indent=2, sort_keys=True)
    except Exception as exc:
        log.debug("Could not write fetch log: %s", exc)


def _seconds_since_fetch(group: str, log_data: dict):
    """Seconds since this group was last fetched, or None if never."""
    stamp = log_data.get(group)
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds()
    except Exception:
        return None


def fetch_group(group: str, session: requests.Session,
                status_out: Optional[dict] = None) -> list:
    """
    Fetch and parse one GP group as JSON.

    An empty result has three very different meanings, and the caller
    needs to tell them apart:

      throttled  CelesTrak already served us this group inside its 2-hour
                 update window. Benign - there is nothing new to fetch.
      invalid    The group name is not one CelesTrak serves. A permanent
                 configuration error. ('noaa' and 'debris' sat in
                 CELESTRAK_GROUPS for months failing this way.)
      error      Network failure or unparseable response.

    Pass a dict as `status_out` to receive {group: reason}.
    """
    def _flag(reason):
        if status_out is not None:
            status_out[group] = reason

    log.info(f"  Fetching {group}...")
    url = _group_url(group, fmt="json")
    text = fetch_url(url, session)
    if not text:
        log.error(f"  Failed to fetch {group}")
        _flag("error")
        return []

    if _CELESTRAK_THROTTLE_MARKER in text:
        log.warning(
            f"  {group}: CelesTrak throttle notice (requested again "
            f"before the 2-hour update window) -- skipping this group "
            f"for this run."
        )
        _flag("throttled")
        return []

    if text.strip().lower().startswith("invalid query"):
        log.error(f"  {group}: CelesTrak does not serve this group -- "
                  f"{text.strip()[:80]}")
        _flag("invalid")
        return []

    try:
        records = _json.loads(text)
    except ValueError as exc:
        log.error(f"  {group}: response was not valid JSON: {exc}")
        _flag("error")
        return []

    parsed = parse_gp_json(records, group)
    log.info(f"  {group}: {len(parsed)} satellites")
    return parsed


# ── Main fetch function ───────────────────────────────────────────────────────

#: Groups worth probing when CELESTRAK_GROUPS turns out to name something
#: CelesTrak does not serve. Debris is catalogued as specific events, not
#: one bucket, which is why a generic "debris" group never existed.
_CANDIDATE_GROUPS = [
    "noaa", "weather", "goes", "resource", "sarsat", "dmc", "tdrss",
    "argos", "planet", "spire", "last-30-days", "analyst",
    "cosmos-1408-debris", "cosmos-2251-debris", "iridium-33-debris",
    "1999-025", "2012-044", "2019-006",
    "science", "geodetic", "engineering", "education", "military",
    "radar", "cubesat", "other", "amateur", "orbcomm", "globalstar",
    "intelsat", "ses", "iridium", "iridium-NEXT", "swarm", "sbas",
]


def check_groups(candidates: Optional[list] = None) -> dict:
    """
    Ask CelesTrak which group names actually resolve.

    CelesTrak answers an unknown group with HTTP 200 and a plain-text
    body ('Invalid query: ... not found'), so a status check cannot catch
    it - only parsing can. 'noaa' and 'debris' were configured for months
    and failed on every run for exactly this reason.

    Returns {group: (ok, detail)}.
    """
    import json as _j

    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePlatform/1.0 (research project)"
    })
    results = {}
    for group in (candidates or list(CELESTRAK_GROUPS)):
        try:
            r = session.get(_group_url(group), timeout=30)
            body = r.text.strip()
            if _CELESTRAK_THROTTLE_MARKER in body:
                # Valid group, fetched recently. Not a fault.
                results[group] = (True, "throttled (fetched within 2h)")
            elif body.lower().startswith("invalid query"):
                results[group] = (False, body[:70])
            else:
                try:
                    data = _j.loads(body)
                    results[group] = (True, f"{len(data)} objects")
                except ValueError:
                    results[group] = (False, f"not JSON: {body[:50]}")
        except Exception as exc:
            results[group] = (False, f"{type(exc).__name__}: {exc}")
        time.sleep(REQUEST_SPACING)
    return results


def fetch_all(groups: Optional[list] = None,
              failures: Optional[list] = None,
              force: bool = False) -> list:
    """
    Fetch GP data from CelesTrak for specified groups (or all if None).
    Returns a deduplicated (by norad_id) list of TLERecord objects.

    Pass a list as `failures` to be told which groups returned nothing.
    A group can fail because CelesTrak throttled the request (transient)
    or because the group name is not one CelesTrak serves (permanent).
    Either way the run should not report unqualified success - two of
    fifteen groups were failing silently on every run until 2026-09-01.
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

    fetch_log = _load_fetch_log()

    for group in target_groups:
        if group not in CELESTRAK_GROUPS:
            log.warning(f"Unknown group: {group} -- skipping")
            continue

        # Do not ask CelesTrak for something it cannot have regenerated yet.
        if not force:
            age = _seconds_since_fetch(group, fetch_log)
            if age is not None and age < MIN_REFETCH_SECONDS:
                mins = (MIN_REFETCH_SECONDS - age) / 60
                log.info(f"  {group}: fetched {age/60:.0f} min ago -- "
                         f"skipping for another {mins:.0f} min "
                         f"(--force to override)")
                continue

        status: dict = {}
        records = fetch_group(group, session, status_out=status)
        reason = status.get(group)
        if reason and reason != "throttled" and failures is not None:
            failures.append(f"{group} ({reason})")

        new_records = []
        for r in records:
            if r.norad_id not in seen_norad:
                seen_norad.add(r.norad_id)
                new_records.append(r)

        if records:
            _record_fetch(group)
        all_records.extend(new_records)

        # Be polite to CelesTrak. With the list trimmed to 5 groups the
        # extra delay costs a few seconds per run and materially lowers
        # the chance of tripping their abuse protection.
        time.sleep(REQUEST_SPACING)

    print(f"\n{'=' * 55}")
    if failures:
        print(f"  WARNING: {len(failures)} of {len(target_groups)} groups "
              f"returned nothing: {', '.join(failures)}")
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

    _write_started = _time.monotonic()

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
        duration_s=_time.monotonic() - _write_started,
    )
    return n


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    # writer.get_engine() reads DATABASE_URL from os.environ, which is right
    # for GitHub Actions. Load .env so manual runs work too.
    try:
        from src.env import load_env
        load_env()
    except ImportError:
        pass

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
    parser.add_argument(
        "--check-groups", action="store_true",
        help="Ask CelesTrak whether each configured group actually exists"
    )
    parser.add_argument(
        "--probe-groups", action="store_true",
        help="Probe ~35 candidate group names. Sends one request each -- "
             "a diagnostic to run rarely, not routinely. Requires --force."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass the 2-hour re-fetch guard, and permit --probe-groups"
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

    if args.probe_groups and not args.force:
        print("\n  --probe-groups sends ~35 requests to CelesTrak in under a")
        print("  minute. CelesTrak asks clients not to re-request data they")
        print("  already hold and blocks persistent offenders.")
        print("\n  This is a one-off diagnostic for when a group name stops")
        print("  resolving - not something to run routinely. If you need it:")
        print("\n      python -m src.tle.fetcher --probe-groups --force\n")
        return None

    if args.check_groups or args.probe_groups:
        which = _CANDIDATE_GROUPS if args.probe_groups else None
        label = "candidate" if args.probe_groups else "configured"
        print(f"\n  Probing {label} groups against CelesTrak...\n")
        results = check_groups(which)
        bad = [g for g, (ok, _) in results.items() if not ok]
        for g, (ok, detail) in results.items():
            print(f"    {'OK  ' if ok else 'BAD '} {g:<22} {detail}")
        print()
        if args.probe_groups:
            good = [g for g, (ok, _) in results.items() if ok]
            print(f"  {len(good)} of {len(results)} candidates are valid.")
        elif bad:
            print(f"  {len(bad)} configured group(s) do not exist: "
                  f"{', '.join(bad)}")
            print("  Run with --probe-groups to find replacements.")
            return None
        else:
            print("  All configured groups resolve.")
        return None

    groups = [args.group] if args.group else None
    _fetch_started = _time.monotonic()
    failed_groups: list = []
    records = fetch_all(groups=groups, failures=failed_groups,
                        force=args.force)
    _fetch_elapsed = _time.monotonic() - _fetch_started

    if args.dry_run:
        print("DRY RUN -- no database write. Showing first 5 records:")
        for r in records[:5]:
            print(f"  {r.norad_id:>6}  {r.name:<30}  {r.regime:<4}  "
                  f"epoch={r.epoch}")
        return records

    run_id = new_run_id()
    # 'partial' is in the schema's status vocabulary and had never been
    # used: a run where some groups failed was indistinguishable from a
    # clean one, which is how noaa and debris failed unnoticed.
    log_step(run_id, pipeline="tle_fetch", step="fetch",
              status="partial" if failed_groups else "success",
              message=(f"groups returned nothing: {', '.join(failed_groups)}"
                       if failed_groups else None),
              records_processed=len(records), source="celestrak",
              duration_s=_fetch_elapsed)

    try:
        n = write_records_to_db(records, run_id)
        print(f"Wrote {n} satellites to database (run_id={run_id}).")
    except Exception as exc:
        log_step(run_id, pipeline="tle_fetch", step="write_db",
                  status="failed", message=str(exc), source="celestrak")
        raise
    # NOTE: a GitHub Actions timeout kills the process outright - no
    # exception, so nothing reaches the except above. That is why the
    # failed writes of 2026-08-26..31 left no 'failed' rows, only an
    # absent write_db step. A missing write_db is therefore a signal in
    # its own right; check_write_speed.py reports those explicitly.

    return records


if __name__ == "__main__":
    main()
