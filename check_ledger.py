"""
Is the Space-Track account actually being protected?

The guard exists: api_request_log.py records every request, and
spacetrack_policy_check.py refuses to run when the plan looks
non-compliant. Both are well built. Neither answers the question this
script asks, which is whether the guard has been *binding* - because a
ledger that some code path never writes to looks identical to a ledger
that records perfect compliance.

It was not binding. On 2026-08-06 the log recorded seven SATCAT requests
against a documented 1/day limit, four of them inside two seconds. That
is either seven requests that should have been one, or logging that
double-counts - and not being able to tell which is itself the problem,
because spacetrack_policy_check.py reads this same log to decide whether
the next run is safe.

So this script reports usage per day against the documented limits, and
- the part the pre-flight check cannot do - looks for a SECOND ledger.
Until 2026-09-05 the Satellite Visibility Tool and the pipeline kept
separate caches under separate roots. Consolidation fixed that; this
check is what notices if it ever comes back.

Read-only. Writes nothing.

    python check_ledger.py
    python check_ledger.py --days 30
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

# Documented Space-Track limits. See docs/API_USAGE_POLICY.md.
LIMIT_SATCAT_PER_DAY = 1
LIMIT_PER_HOUR = 300
LIMIT_PER_MINUTE = 30

#: Places a ledger has lived, or could reappear. A file here that is not
#: the canonical one is a second memory for one account.
LEGACY_LOCATIONS = [
    Path(r"C:\Users\toddl\OneDrive\Data Science Project\Data\TLEs"),
    Path(r"D:\Projects\Satellite Project\Satellite Visibility Tool\data"),
    Path(r"C:\Users\toddl\OneDrive\Data Science Project\Satellite Project"
         r"\Satellite Visibility Tool\data"),
]

LEDGER_NAMES = ["api_request_log.sqlite3", "api_request_log.db"]

#: Caches that must not exist outside TLE_DATA_DIR. A copy left behind
#: after consolidation is not harmless: point a config at it by accident
#: and the account is guarded by a stale memory. The 262 MB
#: tle_history_cache.db under the tool's data/ is exactly this - it held
#: tle_cache.py's schema under tle_history_cache.py's name, and was
#: copied to TLE_DATA_DIR/gp_history_cache.sqlite3 on 2026-09-05.
STRAY_CACHE_NAMES = [
    "tle_history_cache.db", "tle_history_cache.sqlite3",
    "gp_history_cache.db", "gp_history_cache.sqlite3",
    "satcat_cache.db", "satcat_cache.sqlite3",
    "spacetrack_budget.db", "spacetrack_budget.sqlite3",
]


def canonical_dir() -> Path:
    root = os.environ.get("TLE_DATA_DIR")
    if not root:
        sys.exit("TLE_DATA_DIR is not set. Both the tool and the pipeline "
                 "must name the same folder; see docs/API_USAGE_POLICY.md.")
    return Path(root)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14,
                    help="How many days of history to show (default 14)")
    args = ap.parse_args(argv)

    problems: list[str] = []
    root = canonical_dir()
    print(f"\nTLE_DATA_DIR = {root}")
    if not root.exists():
        print("  !! does not exist")
        return 1

    # -- One ledger, or more than one? -----------------------------------
    ledger = None
    for name in LEDGER_NAMES:
        if (root / name).exists():
            ledger = root / name
            break
    if ledger is None:
        problems.append(f"no request ledger in {root}")
        print("  !! no api_request_log found here")
    else:
        print(f"  ledger: {ledger.name} "
              f"({ledger.stat().st_size/1024:,.0f} KB)")

    print("\nOther locations that have held a ledger:")
    strays = 0
    for loc in LEGACY_LOCATIONS:
        for name in LEDGER_NAMES:
            p = loc / name
            try:
                exists = p.exists()
            except OSError:
                continue
            if exists and (ledger is None or p.resolve() != ledger.resolve()):
                strays += 1
                print(f"  !! {p}")
                print(f"     {p.stat().st_size/1024:,.0f} KB - a second "
                      f"memory for one account")
    if strays:
        problems.append(f"{strays} stray ledger(s) outside TLE_DATA_DIR")
    else:
        print("  none - good")

    print("\nStray caches outside TLE_DATA_DIR:")
    orphans = 0
    for loc in LEGACY_LOCATIONS:
        for name in STRAY_CACHE_NAMES:
            q = loc / name
            try:
                if not q.exists() or q.parent.resolve() == root.resolve():
                    continue
            except OSError:
                continue
            orphans += 1
            print(f"  {q}")
            print(f"     {q.stat().st_size/1e6:,.0f} MB - unreferenced since "
                  f"consolidation; safe to delete once the tool has been "
                  f"run once against TLE_DATA_DIR")
    if not orphans:
        print("  none - good")

    # -- The caches that enforce the per-object rules ---------------------
    print("\nCaches in TLE_DATA_DIR:")
    for name in sorted(p.name for p in root.glob("*.sqlite3")):
        size = (root / name).stat().st_size
        print(f"  {name:<34}{size/1e6:>9,.0f} MB")

    if ledger is None:
        return 1

    # -- Usage against the documented limits ------------------------------
    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT timestamp, class FROM requests ORDER BY timestamp").fetchall()
    conn.close()

    if not rows:
        print("\nLedger is empty. Either nothing has run, or something is "
              "making requests without recording them.")
        return 1

    per_day = defaultdict(lambda: defaultdict(int))
    per_hour = defaultdict(int)
    per_minute = defaultdict(int)
    for ts, cls in rows:
        per_day[ts[:10]][cls] += 1
        per_hour[ts[:13]] += 1
        per_minute[ts[:16]] += 1

    days = sorted(per_day)[-args.days:]
    classes = sorted({c for d in per_day.values() for c in d})
    print(f"\nRequests per day (last {len(days)} of {len(per_day)}):")
    header = "  " + "date".ljust(12) + "".join(c.rjust(12) for c in classes)
    print(header)
    for d in days:
        line = "  " + d.ljust(12) + "".join(
            str(per_day[d].get(c, 0)).rjust(12) for c in classes)
        satcat = per_day[d].get("satcat", 0)
        if satcat > LIMIT_SATCAT_PER_DAY:
            line += f"   <-- satcat {satcat}x, limit {LIMIT_SATCAT_PER_DAY}/day"
            problems.append(f"{d}: {satcat} SATCAT requests "
                            f"(limit {LIMIT_SATCAT_PER_DAY}/day)")
        print(line)

    worst_h = max(per_hour.items(), key=lambda kv: kv[1])
    worst_m = max(per_minute.items(), key=lambda kv: kv[1])
    print(f"\nBusiest hour:   {worst_h[0]}  {worst_h[1]} requests "
          f"(limit {LIMIT_PER_HOUR})")
    print(f"Busiest minute: {worst_m[0]}  {worst_m[1]} requests "
          f"(limit {LIMIT_PER_MINUTE})")
    if worst_h[1] > LIMIT_PER_HOUR:
        problems.append(f"{worst_h[0]}: {worst_h[1]} requests in one hour")
    if worst_m[1] > LIMIT_PER_MINUTE:
        problems.append(f"{worst_m[0]}: {worst_m[1]} requests in one minute")

    # -- gp_history is the one that cannot be undone ----------------------
    # "Once per object per lifetime." The cache is the only record of
    # which objects have been spent, so its row count is the meaningful
    # number, not the request count.
    gp_cache = root / "gp_history_cache.sqlite3"
    if gp_cache.exists():
        c = sqlite3.connect(f"file:{gp_cache}?mode=ro", uri=True)
        try:
            n = c.execute("SELECT count(DISTINCT norad) "
                          "FROM tle_history").fetchone()[0]
            print(f"\ngp_history: {n:,} objects already retrieved and cached.")
            print("  Each is spent for the lifetime of the account. Losing "
                  "this file does not restore them.")
        except sqlite3.Error as exc:
            problems.append(f"gp_history cache unreadable: {exc}")
        c.close()
    else:
        print("\n  !! no gp_history_cache.sqlite3 - the 1/object/lifetime "
              "guard has no memory here")
        problems.append("gp_history cache missing from TLE_DATA_DIR")

    print()
    if problems:
        print("PROBLEMS")
        for p in problems:
            print(f"  - {p}")
        print("\nSee docs/API_USAGE_POLICY.md before running anything that "
              "touches Space-Track.")
        return 1
    print("OK - one ledger, and no recorded breach of the documented limits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
