"""
What has appeared in orbit, and what kind of thing is it?

Until 2026-09-05 this script had to infer "new" from `created_at` — the
date the pipeline first saw a row. That conflates three different events:
something new in orbit, something newly published by the source, and
something newly *fetched* because we changed which groups we ask for. It
misclassified 587 COSMOS 2251 fragments as a new launch, when their
designator said 1993 and the collision that made them was in 2009.

SATCAT enrichment replaced the inference with the fact. `launch_date` is
now populated for 96.8% of the catalogue, so "new" means new, and
`object_type` says whether the new thing is a payload, a rocket body or
debris — which is the question actually worth asking.

THE THREE SIGNALS, IN ORDER OF INTEREST
=======================================

  1. NEW LAUNCHES. `launch_date` inside the window. Independent of when
     we first saw the row, so a late-catalogued object still counts and a
     newly-fetched 1993 fragment does not.

  2. FRAGMENTATION. Rows that arrived recently under a launch we already
     held, whose launch is old. A working satellite does not shed parts;
     a 1982 rocket body producing new pieces in 2026 is an event. This is
     the one worth waking up for, and it exits non-zero.

  3. NEWLY VISIBLE. Old launches arriving in bulk. Almost always a change
     in what we fetch — a debris group added — rather than anything
     happening in orbit. Reported, but as configuration, not news.

WHAT THIS STILL CANNOT SEE
==========================
SATCAT lists 35,023 objects in orbit; this catalogue holds ~18,000,
because ingestion fetches CelesTrak's `active` group plus `analyst` and
three named debris events. New launches are caught reliably. Most new
debris is not, and no amount of analysis here fixes a source that was
never asked for it.

Read-only. Writes nothing.

    python check_new_objects.py
    python check_new_objects.py --days 30
    python check_new_objects.py --burst 15
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import text

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

from src.db.writer import get_engine

#: Objects SATCAT lists as ORB — currently in orbit — as of 2026-09-05.
FULL_CATALOGUE_REFERENCE = 35_023

#: Fallback only. `launch_date` is the real test now; this is used for
#: rows SATCAT could not describe (uncatalogued objects have no launch
#: date because they have no catalogue entry).
NEW_LAUNCH_MAX_AGE_YEARS = 2

#: New pieces under an ALREADY-KNOWN launch, above which a human should
#: look. A working satellite does not shed parts.
DEFAULT_BURST_THRESHOLD = 10


def q(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


def launch_year(key: "str | None") -> "int | None":
    """
    The year out of a launch key, or None.

    Kept as the fallback for rows with no `launch_date` — uncatalogued
    analyst tracks, which SATCAT cannot describe. For everything else
    `launch_date` is authoritative and this is not consulted.
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


#: A launch keeps deploying and being catalogued for a while after it
#: happens. Inside this, new payloads are deployment, not an event.
DEPLOYMENT_WINDOW_DAYS = 120

#: Above this share of new pieces arriving on ONE day, an old launch's
#: sudden growth is our ingestion changing rather than an orbital event.
#: A real break-up is tracked and catalogued over days to weeks; 587
#: fragments of a 2009 collision appearing in one afternoon is a debris
#: group being added to the fetch list.
SAME_DAY_INGESTION_FRACTION = 0.9


def classify_arrival(launch_age_days, types, count, same_day_fraction):
    """
    Why did objects from this launch show up now?

    Returns 'deployment', 'fragmentation', 'ingestion_change' or
    'newly_visible'.

    Written against real output. A first version keyed only on "did we
    hold this launch before", and reported HULIANWANG DIGUI payloads from
    a launch 32 days old as fragmentation — because the launch fell just
    outside the reporting window while its payloads arrived just inside
    it. One window was answering two different questions: how recently
    did this launch happen, and how recently did we see the rows.

    `types` is the set of object_type values among the new rows. That is
    the discriminator the catalogue could not supply before 2026-09-05:
    payloads appearing is deployment, debris appearing is an event.
    """
    debris = bool({"DEBRIS", "ROCKET BODY"} & set(types))

    if launch_age_days is not None and \
            launch_age_days <= DEPLOYMENT_WINDOW_DAYS and not debris:
        return "deployment"

    if debris:
        if count >= 20 and same_day_fraction >= SAME_DAY_INGESTION_FRACTION:
            return "ingestion_change"
        return "fragmentation"

    return "newly_visible"


def _type_mix(rows, idx: int) -> str:
    """'12 PAYLOAD, 3 DEBRIS' — what kind of material this actually is."""
    from collections import Counter
    counts = Counter((r[idx] or "unknown") for r in rows)
    return ", ".join(f"{n} {t}" for t, n in counts.most_common())


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
        dated = q(conn,
                  "SELECT count(launch_date) FROM satellites")[0][0]
        print(f"\nCatalogue: {total:,} objects, {dated:,} with a launch date "
              f"({dated*100.0/max(total,1):.1f}%)")
        gap = FULL_CATALOGUE_REFERENCE - total
        if gap > 0:
            print(f"  SATCAT lists {FULL_CATALOGUE_REFERENCE:,} objects in "
                  f"orbit, so roughly {gap:,} — mostly")
            print(f"  debris and rocket bodies — are outside this catalogue "
                  f"and invisible here.")

        # ── 1. New launches, by launch date ───────────────────────────
        launches = q(conn, """
            SELECT norad_id, name, intl_designator, object_type,
                   launch_date, launch_site, created_at::date
              FROM satellites
             WHERE launch_date >= (now() - make_interval(days => :d))::date
             ORDER BY launch_date DESC, norad_id
        """, d=args.days)

        print(f"\n{'='*66}\nNEW LAUNCHES — launched in the last {args.days} "
              f"days\n{'='*66}")
        if not launches:
            print("  none")
        else:
            grouped: dict = {}
            for r in launches:
                grouped.setdefault(launch_key(r[2]) or "(no designator)",
                                   []).append(r)
            lp = "launch" if len(grouped) == 1 else "launches"
            print(f"{len(launches):,} objects across {len(grouped)} {lp}")
            print(f"  {_type_mix(launches, 3)}\n")
            for key, members in sorted(grouped.items(),
                                       key=lambda kv: -len(kv[1])):
                d = members[0][4]
                site = members[0][5] or "-"
                print(f"  {key}   {len(members):>4} object(s)   "
                      f"launched {d}   {site}")
                print(f"      {_type_mix(members, 3)}")
                for norad, name, _, _, _, _, seen in members[:3]:
                    print(f"      {norad:>7}  {(name or '')[:38]:<38}"
                          f"first seen {seen}")
                if len(members) > 3:
                    print(f"      ... and {len(members)-3} more")

            # How long between launch and the catalogue noticing? A
            # widening gap means the pipeline is falling behind, and it
            # is only measurable now that both dates exist.
            lag = q(conn, """
                SELECT min(created_at::date - launch_date),
                       round(avg(created_at::date - launch_date)),
                       max(created_at::date - launch_date)
                  FROM satellites
                 WHERE launch_date >= (now() - make_interval(days => :d))::date
                   AND created_at::date >= launch_date
            """, d=args.days)[0]
            if lag and lag[1] is not None:
                print(f"\n  Launch to first seen: {lag[0]}–{lag[2]} days "
                      f"(mean {lag[1]:.0f})")

        # ── 2 & 3. Rows that arrived recently but are not new ─────────
        arrivals = q(conn, """
            SELECT norad_id, name, intl_designator, object_type,
                   launch_date, created_at::date
              FROM satellites
             WHERE created_at >= now() - make_interval(days => :d)
               AND (launch_date IS NULL
                    OR launch_date < (now() - make_interval(days => :d))::date)
             ORDER BY created_at DESC
        """, d=args.days)

        held_before, fragments, unattributed = {}, [], []
        for r in arrivals:
            key = launch_key(r[2])
            if key is None:
                unattributed.append(r)
            else:
                held_before.setdefault(key, []).append(r)

        from collections import Counter
        buckets = {"deployment": [], "fragmentation": [],
                   "ingestion_change": [], "newly_visible": []}
        for key, members in held_before.items():
            prior = q(conn, """
                SELECT count(*) FROM satellites
                 WHERE intl_designator LIKE :p
                   AND created_at < now() - make_interval(days => :d)
            """, p=key + "%", d=args.days)[0][0]
            ld = members[0][4]
            age = (date.today() - ld).days if ld else None
            seen = Counter(r[5] for r in members)
            frac = seen.most_common(1)[0][1] / len(members)
            kind = classify_arrival(age, {r[3] for r in members},
                                    len(members), frac)
            buckets[kind].append((key, members, prior, age, frac))

        print(f"\n{'='*66}\nOTHER ARRIVALS\n{'='*66}")
        if not any(buckets.values()):
            print("  none")

        def show(kind, title, note):
            rows_ = buckets[kind]
            if not rows_:
                return
            n = sum(len(m) for _, m, _, _, _ in rows_)
            lp = "launch" if len(rows_) == 1 else "launches"
            op = "object" if n == 1 else "objects"
            print(f"\n{title} ({len(rows_)} {lp}, {n:,} {op})")
            for line in note:
                print(f"  {line}")
            print()
            for key, members, prior, age, frac in sorted(
                    rows_, key=lambda x: -len(x[1]))[:10]:
                ld = members[0][4]
                age_s = f"{age:,}d ago" if age is not None else "date unknown"
                print(f"  {key}   +{len(members):<4} (had {prior:,})   "
                      f"launched {ld or '?'} ({age_s})")
                print(f"      {_type_mix(members, 3)}")

        show("fragmentation", "FRAGMENTATION", [
            "New debris or rocket-body pieces under a launch we already "
            "hold.",
            "A working satellite does not shed parts."])
        for key, members, prior, age, frac in buckets["fragmentation"]:
            if len(members) >= args.burst:
                problems.append(
                    f"{key} (launched {members[0][4]}): {len(members)} new "
                    f"pieces under a launch already holding {prior}")

        show("deployment", "DEPLOYMENT", [
            f"Payloads from launches under {DEPLOYMENT_WINDOW_DAYS} days "
            f"old, still being catalogued.",
            "Normal — a launch keeps producing rows for weeks."])

        show("ingestion_change", "INGESTION CHANGE", [
            "Old launches whose pieces nearly all arrived on ONE day.",
            "A real break-up is catalogued over days to weeks; this "
            "shape means",
            "a debris group was added to what we fetch."])

        show("newly_visible", "NEWLY VISIBLE", [
            "Old launches arriving without the signature of either an "
            "event or",
            "a bulk configuration change."])

        # ── Uncatalogued ─────────────────────────────────────────────
        if unattributed:
            print(f"\n{'='*66}\nUNCATALOGUED ({len(unattributed)})\n{'='*66}")
            print("  No international designator, so SATCAT has nothing to "
                  "say about them")
            print("  and they carry no launch date. These are objects being "
                  "tracked but")
            print("  not yet catalogued — the leading edge of new material "
                  "in orbit.\n")
            for norad, name, _, _, _, seen in unattributed[:10]:
                print(f"  {norad:>7}  {(name or 'UNKNOWN')[:34]:<34}{seen}")
            if len(unattributed) > 10:
                print(f"  ... and {len(unattributed)-10} more")

    print()
    if problems:
        print("WORTH A LOOK")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK — nothing above the fragmentation threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
