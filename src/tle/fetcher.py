"""
TLE Fetcher — CelesTrak + Space-Track
======================================
Fetches Two-Line Element sets for all active satellites.

Sources:
  - CelesTrak (primary, no auth required)
  - Space-Track (secondary, requires credentials)

Usage:
    python src/tle/fetcher.py                  # fetch all groups
    python src/tle/fetcher.py --group starlink  # fetch one group
    python src/tle/fetcher.py --dry-run         # print count, no DB write

Environment:
    SPACETRACK_USER=your@email.com
    SPACETRACK_PASS=yourpassword
    DATABASE_URL=postgresql://user:pass@host/db  (Phase 1+)
"""

import os
import time
import logging
import argparse
import requests
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# CelesTrak group URLs — each returns 3LE format (name + 2 TLE lines)
CELESTRAK_GROUPS = {
    "active":       "https://celestrak.org/SOCRATES/query.php?CODE=all&FORMAT=tle",
    "stations":     "https://celestrak.org/SOCRATES/query.php?CODE=stations&FORMAT=tle",
    "visual":       "https://celestrak.org/SOCRATES/query.php?CODE=visual&FORMAT=tle",
    "weather":      "https://celestrak.org/SOCRATES/query.php?CODE=weather&FORMAT=tle",
    "noaa":         "https://celestrak.org/SOCRATES/query.php?CODE=noaa&FORMAT=tle",
    "goes":         "https://celestrak.org/SOCRATES/query.php?CODE=goes&FORMAT=tle",
    "resource":     "https://celestrak.org/SOCRATES/query.php?CODE=resource&FORMAT=tle",
    "starlink":     "https://celestrak.org/SOCRATES/query.php?CODE=starlink&FORMAT=tle",
    "oneweb":       "https://celestrak.org/SOCRATES/query.php?CODE=oneweb&FORMAT=tle",
    "gps-ops":      "https://celestrak.org/SOCRATES/query.php?CODE=gps-ops&FORMAT=tle",
    "glo-ops":      "https://celestrak.org/SOCRATES/query.php?CODE=glo-ops&FORMAT=tle",
    "galileo":      "https://celestrak.org/SOCRATES/query.php?CODE=galileo&FORMAT=tle",
    "beidou":       "https://celestrak.org/SOCRATES/query.php?CODE=beidou&FORMAT=tle",
    "geo":          "https://celestrak.org/SOCRATES/query.php?CODE=geo&FORMAT=tle",
    "debris":       "https://celestrak.org/SOCRATES/query.php?CODE=debris&FORMAT=tle",
}

# Simpler direct TLE URLs (more reliable)
CELESTRAK_TLE_URLS = {
    "active":    "https://celestrak.org/SOCRATES/query.php?CODE=all&FORMAT=tle",
    "stations":  "https://celestrak.org/pub/TLE/stations.txt",
    "starlink":  "https://celestrak.org/SOCRATES/query.php?CODE=starlink&FORMAT=tle",
    "gps-ops":   "https://celestrak.org/pub/TLE/gps-ops.txt",
    "glo-ops":   "https://celestrak.org/pub/TLE/glo-ops.txt",
    "geo":       "https://celestrak.org/pub/TLE/geo.txt",
    "visual":    "https://celestrak.org/pub/TLE/visual.txt",
    "weather":   "https://celestrak.org/pub/TLE/weather.txt",
    "resource":  "https://celestrak.org/pub/TLE/resource.txt",
    "oneweb":    "https://celestrak.org/pub/TLE/oneweb.txt",
    "starlink":  "https://celestrak.org/pub/TLE/starlink.txt",
    "gps-ops":   "https://celestrak.org/pub/TLE/gps-ops.txt",
}

# Primary reliable URLs
PRIMARY_URLS = {
    "active":   "https://celestrak.org/pub/TLE/active.txt",
    "stations": "https://celestrak.org/pub/TLE/stations.txt",
    "starlink": "https://celestrak.org/pub/TLE/starlink.txt",
    "gps-ops":  "https://celestrak.org/pub/TLE/gps-ops.txt",
    "glo-ops":  "https://celestrak.org/pub/TLE/glo-ops.txt",
    "geo":      "https://celestrak.org/pub/TLE/geo.txt",
    "visual":   "https://celestrak.org/pub/TLE/visual.txt",
    "weather":  "https://celestrak.org/pub/TLE/weather.txt",
    "resource": "https://celestrak.org/pub/TLE/resource.txt",
    "oneweb":   "https://celestrak.org/pub/TLE/oneweb.txt",
}

REQUEST_TIMEOUT = 30   # seconds
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5    # seconds between retries


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TLERecord:
    """A single satellite TLE record."""
    name:       str
    line1:      str
    line2:      str
    norad_id:   int
    epoch:      str
    regime:     str
    group:      str
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def intl_designator(self) -> str:
        """International designator from TLE line 1."""
        return self.line1[9:17].strip()

    @property
    def inclination(self) -> float:
        """Orbital inclination in degrees."""
        try:
            return float(self.line2[8:16])
        except ValueError:
            return 0.0

    @property
    def mean_motion(self) -> float:
        """Mean motion in revolutions per day."""
        try:
            return float(self.line2[52:63])
        except ValueError:
            return 0.0

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "line1":        self.line1,
            "line2":        self.line2,
            "norad_id":     self.norad_id,
            "epoch":        self.epoch,
            "regime":       self.regime,
            "group":        self.group,
            "inclination":  self.inclination,
            "mean_motion":  self.mean_motion,
            "intl_desig":   self.intl_designator,
            "fetched_at":   self.fetched_at.isoformat(),
        }


# ── Regime classifier ─────────────────────────────────────────────────────────

def classify_regime(mean_motion: float, inclination: float) -> str:
    """
    Classify orbital regime from mean motion (rev/day) and inclination.

    Mean motion → approximate altitude:
      > 11.25  →  LEO  (< ~2000 km)
      1.0–11.25 → MEO  (2000–35786 km)
      ~1.0027  →  GEO  (~35786 km, geostationary)
      < 1.0    →  HEO  (highly elliptical)
    """
    if mean_motion > 11.25:
        return "LEO"
    elif mean_motion >= 2.0:
        return "MEO"
    elif 0.9 <= mean_motion <= 1.1:
        return "GEO"
    else:
        return "HEO"


# ── TLE parser ────────────────────────────────────────────────────────────────

def parse_norad_id(line1: str) -> int:
    """Extract NORAD catalog ID from TLE line 1."""
    try:
        return int(line1[2:7].strip())
    except (ValueError, IndexError):
        return 0


def parse_epoch(line1: str) -> str:
    """Extract epoch string from TLE line 1."""
    try:
        return line1[18:32].strip()
    except IndexError:
        return ""


def parse_tle_text(text: str, group: str) -> list[TLERecord]:
    """
    Parse 3LE format text into TLERecord objects.

    3LE format:
        SATELLITE NAME
        1 NNNNNC NNNNNAAA NNNNN.NNNNNNNN .NNNNNNNN NNNNN-N NNNNN-N N NNNNN
        2 NNNNN NNN.NNNN NNN.NNNN NNNNNNN NNN.NNNN NNN.NNNN NN.NNNNNNNNNNNN
    """
    records = []
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    i = 0
    while i < len(lines) - 2:
        name  = lines[i]
        line1 = lines[i + 1]
        line2 = lines[i + 2]

        # Validate TLE lines
        if not (line1.startswith("1 ") and line2.startswith("2 ")):
            i += 1
            continue

        norad_id    = parse_norad_id(line1)
        epoch       = parse_epoch(line1)

        # Build record
        rec = TLERecord(
            name=name,
            line1=line1,
            line2=line2,
            norad_id=norad_id,
            epoch=epoch,
            regime="",   # set after mean_motion is available
            group=group,
        )
        rec.regime = classify_regime(rec.mean_motion, rec.inclination)
        records.append(rec)
        i += 3

    return records


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


def fetch_group(group: str, url: str,
                session: requests.Session) -> list[TLERecord]:
    """Fetch and parse one TLE group."""
    log.info(f"  Fetching {group}...")
    text = fetch_url(url, session)
    if not text:
        log.error(f"  Failed to fetch {group}")
        return []

    records = parse_tle_text(text, group)
    log.info(f"  {group}: {len(records)} satellites")
    return records


# ── Main fetch function ───────────────────────────────────────────────────────

def fetch_all(groups: Optional[list[str]] = None,
              dry_run: bool = False) -> list[TLERecord]:
    """
    Fetch TLEs from CelesTrak for specified groups (or all if None).
    Returns list of TLERecord objects.
    """
    target_groups = groups or list(PRIMARY_URLS.keys())
    all_records: list[TLERecord] = []
    seen_norad: set[int] = set()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePlatform/1.0 (research project)"
    })

    print(f"\n{'═'*55}")
    print(f"  TLE Fetcher — CelesTrak")
    print(f"  Groups: {', '.join(target_groups)}")
    if dry_run:
        print(f"  MODE: DRY RUN — no database writes")
    print(f"{'═'*55}\n")

    for group in target_groups:
        url = PRIMARY_URLS.get(group)
        if not url:
            log.warning(f"Unknown group: {group} — skipping")
            continue

        records = fetch_group(group, url, session)

        # Deduplicate by NORAD ID across groups
        new_records = []
        for r in records:
            if r.norad_id not in seen_norad:
                seen_norad.add(r.norad_id)
                new_records.append(r)

        all_records.extend(new_records)

        # Rate limiting — be polite to CelesTrak
        time.sleep(1)

    print(f"\n{'═'*55}")
    print(f"  Total unique satellites: {len(all_records)}")

    # Regime breakdown
    regimes = {}
    for r in all_records:
        regimes[r.regime] = regimes.get(r.regime, 0) + 1
    for regime, count in sorted(regimes.items()):
        print(f"  {regime:>4}: {count:,}")
    print(f"{'═'*55}\n")

    return all_records


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="TLE Fetcher — fetch satellite TLEs from CelesTrak"
    )
    parser.add_argument(
        "--group", type=str,
        help=f"Fetch one group only. Options: {', '.join(PRIMARY_URLS.keys())}"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and parse but do not write to database"
    )
    parser.add_argument(
        "--list-groups", action="store_true",
        help="List available groups and exit"
    )
    args = parser.parse_args()

    if args.list_groups:
        print("\nAvailable CelesTrak groups:")
        for g, url in PRIMARY_URLS.items():
            print(f"  {g:<12} {url}")
        return

    groups = [args.group] if args.group else None
    records = fetch_all(groups=groups, dry_run=args.dry_run)

    if args.dry_run:
        print("DRY RUN — showing first 5 records:")
        for r in records[:5]:
            print(f"  {r.norad_id:>6}  {r.name:<30}  {r.regime:<4}  "
                  f"epoch={r.epoch}")

    return records


if __name__ == "__main__":
    main()
