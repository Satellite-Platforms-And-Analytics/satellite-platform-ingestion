"""
Why is tle_history 515 MB?

It is the largest object in the database by a factor of ten and, unlike
orbital_positions and visibility_windows, nothing prunes it. Before adding
a retention policy it is worth knowing whether the rows are real - genuine
new element sets - or the same element sets arriving repeatedly under
epochs that differ just enough to defeat the UNIQUE (norad_id, epoch)
constraint.

The two look identical from the outside and want opposite fixes:

  * Real churn        -> the table is doing its job; it needs retention.
  * Defeated dedupe   -> a bug in what is written; retention would only
                         slow the bleeding.

The distinguishing measurement is distinct epochs per satellite per day
against the fetch cadence. The fetcher runs every 2 hours (12x/day). If
satellites average ~12 new epochs a day, the epochs are tracking when we
asked rather than when the element set was issued. Real CelesTrak churn
for a typical object is more like 1-4 a day.

Read-only. Writes nothing, deletes nothing.
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


def q(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


def main() -> int:
    with get_engine().connect() as conn:

        # ── Size and shape ────────────────────────────────────────────
        total, sats, first, last = q(conn, """
            SELECT count(*), count(DISTINCT norad_id), min(epoch), max(epoch)
              FROM tle_history
        """)[0]
        size = q(conn, "SELECT pg_total_relation_size('tle_history')")[0][0]
        heap = q(conn, "SELECT pg_relation_size('tle_history')")[0][0]

        print(f"\ntle_history: {size/1e6:,.0f} MB total "
              f"({heap/1e6:,.0f} MB rows, {(size-heap)/1e6:,.0f} MB indexes)")
        print(f"  {total:,} rows over {sats:,} satellites "
              f"= {total/max(sats,1):,.0f} element sets each")
        print(f"  {size/max(total,1):,.0f} bytes per row")
        print(f"  epochs span {first} .. {last}")

        # ── Indexes. Four of them on this table; one is redundant. ────
        print("\nIndexes:")
        for name, isize, defn in q(conn, """
            SELECT indexname, pg_relation_size(indexname::regclass), indexdef
              FROM pg_indexes
             WHERE tablename = 'tle_history'
             ORDER BY 2 DESC
        """):
            print(f"  {name:<34} {isize/1e6:7.1f} MB")
            print(f"      {defn.split('USING')[-1].strip()}")

        # ── The verdict measurement ───────────────────────────────────
        # Distinct epochs per satellite per day. Compare to 12 (the
        # 2-hourly fetch cadence) versus 1-4 (real element set churn).
        rows = q(conn, """
            SELECT epoch::date AS d,
                   count(*)                       AS rows_that_day,
                   count(DISTINCT norad_id)       AS sats_that_day,
                   count(*)::float
                     / NULLIF(count(DISTINCT norad_id), 0) AS per_sat
              FROM tle_history
             GROUP BY 1
             ORDER BY 1 DESC
             LIMIT 14
        """)
        print("\nNew element sets per day (by epoch date):")
        print(f"  {'date':<12}{'rows':>10}{'satellites':>12}{'per sat':>10}")
        for d, n, s, per in rows:
            flag = "  <-- tracks the 12x/day fetch cadence" if per and per > 8 else ""
            print(f"  {str(d):<12}{n:>10,}{s:>12,}{per or 0:>10.1f}{flag}")

        # ── Same question from the other side ─────────────────────────
        # If dedupe is being defeated, one satellite will show many epochs
        # clustered minutes apart rather than a handful spread over hours.
        busiest = q(conn, """
            SELECT norad_id, count(*) AS n
              FROM tle_history
             GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """)
        if busiest:
            norad, n = busiest[0]
            print(f"\nBusiest satellite: {norad} with {n:,} element sets.")
            print("  Last 12 epochs and the gap between them:")
            prev = None
            for (e,) in q(conn, """
                SELECT epoch FROM tle_history
                 WHERE norad_id = :n ORDER BY epoch DESC LIMIT 12
            """, n=norad):
                gap = f"{(prev - e).total_seconds()/3600:6.2f} h" if prev else "     -"
                print(f"    {e}   {gap}")
                prev = e

        # ── Near-duplicate epochs ─────────────────────────────────────
        # A TLE epoch carries ~0.86 ms of real precision. Two rows for the
        # same satellite less than a second apart are the same element set
        # rendered twice - the same object arriving in two of the five
        # groups we fetch, each rounding EPOCH slightly differently.
        # UNIQUE (norad_id, epoch) cannot see it because the timestamps
        # do differ. This counts what that costs.
        print("\nNear-duplicate epochs (same satellite, <1s apart):")
        for label, window in (("< 1 ms", "0.001"), ("< 1 s", "1"),
                              ("< 60 s", "60")):
            n = q(conn, """
                SELECT count(*) FROM (
                    SELECT epoch - lag(epoch) OVER (PARTITION BY norad_id
                                                    ORDER BY epoch) AS gap
                      FROM tle_history
                ) t
                WHERE gap IS NOT NULL
                  AND gap < make_interval(secs => :w)
            """, w=float(window))[0][0]
            print(f"  {label:<8}{n:>10,} rows "
                  f"({n*100.0/max(total,1):4.1f}%, "
                  f"{n*size/max(total,1)/1e6:5.0f} MB)")

        # ── What retention would buy ──────────────────────────────────
        print("\nIf tle_history were pruned:")
        for days in (7, 14, 30, 60):
            kept = q(conn, """
                SELECT count(*) FROM tle_history
                 WHERE epoch >= now() - make_interval(days => :d)
            """, d=days)[0][0]
            print(f"  keep {days:>2} days: {kept:>10,} rows "
                  f"= {kept*size/max(total,1)/1e6:6.0f} MB "
                  f"({(total-kept)*100.0/max(total,1):4.1f}% reclaimed)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
