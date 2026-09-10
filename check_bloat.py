"""
How much of the database is space that nothing is using?

WHY THIS EXISTS
===============
On 2026-09-01 tle_history was found at 515 MB of a 597 MB database. The
rows were pruned and nothing was reclaimed, because DELETE only marks
tuples dead — their space stays in the file and Supabase bills on the
file. `cleanup_tle_history.py` was written with a VACUUM FULL step and a
paragraph explaining exactly that.

The lesson was applied to the table where it was found and nowhere else.

On 2026-09-10 the database read **469 MB of a 500 MB tier** — 94% — with
`visibility_windows` at 257.8 MB against 193 MB projected for its own
retention window. It is pruned daily by `--prune` and **has never been
vacuumed**. Nothing in the repository vacuums anything but tle_history.

So this reports the whole class rather than one table: what each table
costs, how much of it is dead, and when it was last actually reclaimed.

WHAT AUTOVACUUM DOES AND DOES NOT DO
====================================
Supabase runs autovacuum, so `n_dead_tup` is often low — it marks space
reusable *within the file*. It does not shrink the file. A table can show
almost no dead tuples and still be carrying hundreds of megabytes of
free pages that Postgres will happily refill but the tier still counts.

That is why `last_vacuum` matters separately from `last_autovacuum`:
only a manual VACUUM FULL returns space to the operating system, and a
NULL there on a heavily-pruned table is the finding.

Read-only. Writes nothing, reclaims nothing.

    python check_bloat.py
"""
from __future__ import annotations

import sys

from sqlalchemy import text

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

from src.db.writer import get_engine

#: Supabase free tier. 500 MB decimal, not MiB — the first version of
#: this file wrote 500 * 1024 * 1024 and then printed it as "524 MB",
#: which understated fullness by 5% in the direction of comfort.
TIER_BYTES = 500_000_000

#: Fraction of the tier above which this exits non-zero.
FULL_FRACTION = 0.85

#: Tables pruned on a schedule: the date column to measure, and the
#: retention the code intends. Days-held against days-intended is the
#: check that matters — see the note below about why last_vacuum is not.
PRUNED_TABLES = {
    "visibility_windows": ("analysis_date", 3,
                           "compute.DEFAULT_RETENTION_DAYS"),
    "tle_history":        ("epoch::date", 14,
                           "prune_old_tle_history(days=14)"),
    "orbital_positions":  ("timestamp::date", 2,
                           "prune_old_positions(), 48h"),
}

#: WHY last_vacuum IS NOT THE CHECK
#:
#: The first version of this script flagged every pruned table whose
#: last_vacuum was NULL as "pruned, never reclaimed". That column only
#: records MANUAL vacuums, so it reads NULL for every table in the
#: database including empty ones — it cannot distinguish a bloated table
#: from an untouched one.
#:
#: The 2026-09-10 run made that obvious: it flagged all three pruned
#: tables while their dead-tuple counts were 0.0%, 2.5% and 7.7%.
#: Autovacuum had been doing its job the whole time and there was almost
#: nothing to reclaim. A check whose output looks authoritative and is
#: not measuring what it claims is the shape this project keeps finding;
#: this one lasted twenty minutes.
#:
#: Dead-tuple percentage is the honest bloat signal. Days held against
#: retention is the honest retention signal. Both are below.


def main(argv=None) -> int:
    problems: list[str] = []

    with get_engine().connect() as conn:
        total = conn.execute(text(
            "SELECT pg_database_size(current_database())")).scalar()
        pct = total * 100.0 / TIER_BYTES
        print(f"\nDatabase: {total/1e6:,.0f} MB of a "
              f"{TIER_BYTES/1e6:,.0f} MB tier ({pct:.0f}%)")
        print(f"  headroom: {(TIER_BYTES - total)/1e6:,.0f} MB")
        if total > TIER_BYTES * FULL_FRACTION:
            problems.append(
                f"database is at {pct:.0f}% of the tier "
                f"({(TIER_BYTES-total)/1e6:,.0f} MB free)")

        rows = conn.execute(text("""
            SELECT relname,
                   n_live_tup, n_dead_tup,
                   pg_total_relation_size(relid) AS total,
                   pg_relation_size(relid)       AS heap,
                   last_vacuum, last_autovacuum
              FROM pg_stat_user_tables
             ORDER BY pg_total_relation_size(relid) DESC
        """)).fetchall()

        print(f"\n{'table':<24}{'total':>10}{'live (est)':>12}"
              f"{'dead':>10}{'dead%':>8}")
        for (name, live, dead, tot, heap, lv, lav) in rows:
            if not tot or tot < 1e6:
                continue
            frac = dead * 100.0 / max(live + dead, 1)
            flag = "  <-- worth reclaiming" if frac >= 20 else ""
            if frac >= 20:
                problems.append(
                    f"{name}: {frac:.0f}% dead tuples over {tot/1e6:,.0f} MB")
            print(f"  {name:<22}{tot/1e6:>9,.0f}M{live:>12,}{dead:>10,}"
                  f"{frac:>7.1f}%{flag}")
        print("  (live is pg_stat's estimate; the retention table below "
              "counts exactly)")

        # ── Retention: is each table holding what it intends to? ──────
        # The actionable number. On 2026-09-10 visibility_windows held
        # exactly 4.0 days against a 3-day setting — 65 MB of a 44 MB
        # headroom, and not a vacuum problem at all.
        print(f"\n{'table':<24}{'dates':>7}{'expect':>8}{'window':>8}"
              f"{'rows':>12}{'over':>7}")
        print("  (a rolling N-day window spans N+1 dates; 'expect' is N+1)")
        for name, (datecol, keep, where) in PRUNED_TABLES.items():
            try:
                held, n = conn.execute(text(
                    f"SELECT count(DISTINCT {datecol}), count(*) "
                    f"FROM {name}")).fetchone()
            except Exception as exc:
                print(f"  {name:<22}unreadable: {exc}")
                continue
            # A rolling N-day window spans N+1 calendar dates by
            # construction: at 21:44 Wednesday, 48 hours reaches back to
            # 21:44 Monday and touches Mon/Tue/Wed. The first version of
            # this compared distinct dates against N and reported all
            # three tables as over-retained. They were not — that is what
            # the retention setting means.
            #
            # What IS worth flagging is the gap between that and what
            # report_budget projects, which multiplies per-day by N and
            # so understates the real cost by one day's worth.
            expected = keep + 1
            over = held - expected
            per_day = n / max(held, 1)
            excess = int(per_day * over) if over > 0 else 0
            flag = ""
            if over > 0:
                sz = next((t for (nm, _, _, t, _, _, _) in rows
                           if nm == name), 0)
                mb = (sz / max(n, 1)) * excess / 1e6
                flag = f"  ~{mb:,.0f} MB"
                problems.append(
                    f"{name}: holding {held} calendar dates, more than the "
                    f"{expected} a {keep}-day window implies ({where}) — "
                    f"about {excess:,} rows, ~{mb:,.0f} MB")
            print(f"  {name:<22}{held:>7}{expected:>8}{keep:>7}d{n:>12,}"
                  f"{over:>7}{flag}")

        # Bytes per live row is the number that exposes a bloated file
        # even when autovacuum has kept the dead-tuple count low: the
        # space is reusable but still counted against the tier.
        print("\nBytes per live row (a jump here is free space in the "
              "file, not data):")
        for (name, live, dead, tot, heap, lv, lav) in rows:
            if live and tot:
                print(f"  {name:<22}{tot/live:>8,.0f} bytes/row"
                      f"   {live:>10,} live rows")

    print()
    if problems:
        print("PROBLEMS")
        for p in problems:
            print(f"  - {p}")
        print("\nA table holding more days than it intends is a "
              "retention problem — prune it.")
        print("A table with a high dead-tuple share is a reclaim problem "
              "— VACUUM FULL,")
        print("which takes an ACCESS EXCLUSIVE lock and rewrites the "
              "table; see")
        print("cleanup_tle_history.py, including why "
              "raw_connection().autocommit does not work.")
        print("They are different problems and only one of them is fixed "
              "by vacuuming.")
        return 1

    print("OK - nothing pruned-but-unreclaimed, and the tier has room.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
