"""
What has appeared in orbit since we last looked?

Every row in `satellites` carries `created_at`, set by the column default
on INSERT. `upsert_satellites` updates `last_updated` on conflict and
never touches `created_at`, so it is already a genuine first-seen
timestamp for every object the pipeline has ever recorded. Nothing read
it until this script.

WHAT THIS ANSWERS
=================
Three different questions that look the same in a row count:

  1. A NEW LAUNCH. Objects arrive carrying an international designator
     whose launch has never been seen before. A rideshare deploying
     forty payloads is forty new rows sharing one launch key.

  2. A FRAGMENTATION. Objects arrive under a launch key we ALREADY
     hold. A 1970s rocket body does not deploy new payloads in 2026;
     new pieces under an old launch mean something broke up. This is
     the signal worth waking up for, and the reason clustering by
     designator matters more than counting rows.

  3. CATALOGUE CHURN. An object we simply had not fetched yet -
     re-classified, or newly published by the source. Not an event.

The international designator does the work: `YYYY-NNNAAA`, where
`YYYY-NNN` identifies the launch and the trailing letters identify the
piece. Every fragment of an object inherits its parent's launch.

WHAT THIS CANNOT SEE - READ THIS BEFORE TRUSTING IT
===================================================
The catalogue holds ~18,000 objects. A full Space-Track GP snapshot on
2026-08-07 held **31,651**. The gap is roughly 13,600 objects, almost
entirely debris and rocket bodies, because ingestion fetches CelesTrak's
`active` group plus `analyst` and three named debris events - and
`active` means active payloads.

So today this script reliably catches **new launches** and **fragments
of the three debris events already configured**, and will miss most
other new debris entirely. That is a source limitation, not a bug here,
and closing it means changing where the catalogue comes from - see
docs/API_USAGE_POLICY.md on the GP class, which is one request per hour
for the whole catalogue.

Read-only. Writes nothing.

    python check_new_objects.py
    python check_new_objects.py --days 30
    python check_new_objects.py --burst 15     # fail if a launch gains 15+
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

from src.db.writer import get_engine

#: Objects SATCAT lists as ORB - currently in orbit - as of the survey on
#: 2026-09-05 (35,023 of 70,580 records; the rest are IMP/impacted, LAN
#: and DOC). Used only to state the coverage gap honestly rather than
#: implying completeness.
FULL_CATALOGUE_REFERENCE = 35_023

#: A launch designated more than this many years ago is not a new launch,
#: whatever the catalogue's first-seen date says.
NEW_LAUNCH_MAX_AGE_YEARS = 2

#: New pieces under an ALREADY-KNOWN launch, above which something is
#: worth a human look. A working satellite does not shed parts.
DEFAULT_BURST_THRESHOLD = 10


def q(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


def launch_year(key: "str | None") -> "int | None":
    """
    The year out of a launch key, or None.

    This is the correction to a real false positive. On 2026-09-01
    ingestion began fetching CelesTrak's debris groups, and 587 COSMOS
    2251 fragments plus 111 IRIDIUM 33 fragments arrived at once. The
    catalogue had held none of them, so "no prior objects from this
    launch" reported them as NEW LAUNCHES - when their designators say
    1993-036 and 1997-051, and the collision that produced them was in
    2009.

    "First seen by us" conflates three different things: new to the
    world, newly published by the source, and newly *fetched* because we
    changed which groups we ask for. The designator year separates the
    first from the other two at no cost.
    """
    if not key or len(key) < 4 or not key[:4].isdigit():
        return None
    return int(key[:4])


def launch_key(intl_designator: "str | None") -> "str | None":
    """
    'YYYY-NNNAAA' -> 'YYYY-NNN', the launch every piece inherits.

    Returns None for anything that does not match, so an unparseable
    designator is reported separately rather than silently forming its
    own bogus launch. Analyst objects and uncatalogued tracks land here,
    and those are exactly the ones worth looking at by hand.
    """
    if not intl_designator:
        return None
    key = intl_designator.strip()[:8]
    if len(key) != 8 or key[4] != "-":
        return None
    if not key[:4].isdigit() or not key[5:8].isdigit():
        return None
    return key


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14,
                    help="How far back to look (default 14)")
    ap.add_argument("--burst", type=int, default=DEFAULT_BURST_THRESHOLD,
                    help="Fragments under one known launch before this "
                         "exits non-zero (default %(default)s)")
    args = ap.parse_args(argv)

    problems: list[str] = []

    with get_engine().connect() as conn:
        total = q(conn, "SELECT count(*) FROM satellites")[0][0]
        print(f"\nCatalogue: {total:,} objects")
        gap = FULL_CATALOGUE_REFERENCE - total
        if gap > 0:
            print(f"  A full GP snapshot held {FULL_CATALOGUE_REFERENCE:,}, "
                  f"so roughly {gap:,} objects - mostly debris and rocket "
                  f"bodies -")
            print(f"  are not in this catalogue and cannot be detected here.")

        # -- Is the earliest day a backfill? -------------------------
        # The initial load gives thousands of rows one identical
        # created_at. Counting that as "new objects" would be nonsense,
        # so name it instead.
        first_day, first_n = q(conn, """
            SELECT created_at::date, count(*)
              FROM satellites
             GROUP BY 1 ORDER BY 1 ASC LIMIT 1
        """)[0]
        print(f"\n  First-seen dates begin {first_day} with {first_n:,} "
              f"objects")
        if first_n > 1000:
            print(f"  (that is the initial backfill, not a launch)")

        # -- Arrivals per day ----------------------------------------
        rows = q(conn, """
            SELECT created_at::date AS d, count(*)
              FROM satellites
             WHERE created_at >= now() - make_interval(days => :d)
             GROUP BY 1 ORDER BY 1 DESC
        """, d=args.days)
        print(f"\nFirst seen in the last {args.days} days:")
        if not rows:
            print("  nothing - no new objects recorded in this window")
        for day, n in rows:
            print(f"  {str(day):<12}{n:>6}")

        recent = q(conn, """
            SELECT norad_id, name, intl_designator, created_at::date
              FROM satellites
             WHERE created_at >= now() - make_interval(days => :d)
             ORDER BY created_at DESC
        """, d=args.days)
        if not recent:
            print("\nNothing new in this window.")
            return 0

        # -- Cluster by launch ---------------------------------------
        # 'YYYY-NNNAAA' -> 'YYYY-NNN' is the launch; the rest is the
        # piece. Anything that does not parse is grouped separately
        # rather than silently dropped.
        launches: dict = {}
        unparsed = []
        for norad, name, intl, day in recent:
            key = launch_key(intl)
            if key:
                launches.setdefault(key, []).append((norad, name, day))
            else:
                unparsed.append((norad, name, intl, day))

        print(f"\n{len(recent):,} new objects across {len(launches)} launch(es)")

        this_year = q(conn, "SELECT extract(year FROM now())::int")[0][0]
        new_launches, fragmentations, newly_visible = [], [], []
        for key, members in launches.items():
            # How many objects from this launch did we already hold
            # before this window?
            prior = q(conn, """
                SELECT count(*) FROM satellites
                 WHERE intl_designator LIKE :p
                   AND created_at < now() - make_interval(days => :d)
            """, p=key + "%", d=args.days)[0][0]
            yr = launch_year(key)
            recent_launch = (yr is not None and
                             yr >= this_year - NEW_LAUNCH_MAX_AGE_YEARS)
            if prior == 0 and recent_launch:
                new_launches.append((key, members, prior))
            elif prior == 0:
                # An old launch we simply did not hold before. Almost
                # always a change in what WE fetch, not an orbital event.
                newly_visible.append((key, members, prior))
            else:
                fragmentations.append((key, members, prior))

        if new_launches:
            print(f"\nNEW LAUNCHES ({len(new_launches)}):")
            for key, members, _ in sorted(new_launches,
                                          key=lambda x: -len(x[1])):
                print(f"  {key}   {len(members):>4} object(s)"
                      f"   first seen {members[0][2]}")
                for norad, name, _ in members[:4]:
                    print(f"      {norad:>7}  {name}")
                if len(members) > 4:
                    print(f"      ... and {len(members)-4} more")

        if newly_visible:
            total_nv = sum(len(m) for _, m, _ in newly_visible)
            print(f"\nOLD LAUNCHES, NEWLY VISIBLE TO US "
                  f"({len(newly_visible)} launches, {total_nv:,} objects):")
            print("  These carry designators from previous years, so they "
                  "are not new")
            print("  launches. Objects arriving in bulk under old "
                  "designators normally")
            print("  mean the ingestion configuration changed - a debris "
                  "group added,")
            print("  say - rather than anything happening in orbit.")
            for key, members, _ in sorted(newly_visible,
                                          key=lambda x: -len(x[1])):
                print(f"  {key}   {len(members):>4} object(s)"
                      f"   first seen {members[0][2]}")
                for norad, name, _ in members[:2]:
                    print(f"      {norad:>7}  {name}")

        if fragmentations:
            print(f"\nNEW PIECES UNDER EXISTING LAUNCHES "
                  f"({len(fragmentations)}):")
            print("  A launch we already hold gaining new pieces is either "
                  "a deployment")
            print("  from a recent launch, or a fragmentation of something "
                  "older.")
            for key, members, prior in sorted(fragmentations,
                                              key=lambda x: -len(x[1])):
                flag = ""
                if len(members) >= args.burst:
                    flag = f"   <-- {len(members)} new pieces"
                    problems.append(
                        f"{key}: {len(members)} new pieces under a launch "
                        f"already holding {prior} - possible fragmentation")
                print(f"  {key}   +{len(members):<4} (had {prior:,})"
                      f"   {members[0][2]}{flag}")
                for norad, name, _ in members[:3]:
                    print(f"      {norad:>7}  {name}")

        if unparsed:
            print(f"\nNo usable international designator ({len(unparsed)}):")
            for norad, name, intl, day in unparsed[:10]:
                print(f"  {norad:>7}  {(name or '')[:34]:<34}"
                      f"{intl or '(none)'}  {day}")
            print("  These cannot be attributed to a launch. Analyst objects "
                  "and")
            print("  uncatalogued tracks look like this and are worth "
                  "watching.")

    print()
    if problems:
        print("WORTH A LOOK")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK - nothing above the fragmentation threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
