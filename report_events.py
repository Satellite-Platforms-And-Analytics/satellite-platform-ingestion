"""
What did the catalogue notice that it had not noticed before?

`check_new_objects.py --record` writes everything it finds in a rolling
30-day window. Run daily, that re-detects the same nineteen launches every
morning for a month. A digest built on "everything notable in the window"
would therefore say the same thing every day, and a report that says the
same thing every day is one nobody reads — the failure mode this project
has already paid for seven times over.

So this reports only what is NEW. `catalog_events.detected_at` is set by
the column default on INSERT and is deliberately absent from the upsert's
DO UPDATE list, so it records when a finding was FIRST seen and never
moves. A re-detection bumps `updated_at` and leaves `detected_at` alone.
That one distinction is what lets the monitor be silent on a quiet day.

Exit code is 0 whether or not there is anything to report — this is a
reporter, not a guard. `check_new_objects.py` decides what is a problem.

    python report_events.py                 # last 24 hours, to stdout
    python report_events.py --since-hours 168
    python report_events.py --out digest.md
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

#: Event types that occur EVERY day by construction. The two health
#: metrics are written once per run with an event_key of
#: '<metric>:<date>', so their detected_at is always today and they are
#: always "new".
#:
#: That made the monitor incapable of silence. It opened an issue on
#: 2026-09-06, 07, 08 and 09 — and the 09-09 one contained nothing but
#: these two lines and the words "0 notable". A daily issue that usually
#: says nothing is the alert-fatigue form of a swallowed error, and it is
#: precisely what this design was supposed to avoid.
#:
#: So they are context, not news: they appear IN a digest, but they no
#: longer cause one. A metric that crosses its threshold sets notable and
#: speaks for itself.
ROUTINE_TYPES = {"latency_regression", "uncatalogued_growth"}

#: Headings, in the order a reader should meet them: the thing that needs
#: a human first, then context, then housekeeping.
SECTIONS = [
    ("fragmentation", "Fragmentation",
     "New debris or rocket-body pieces under a launch already in the "
     "catalogue. A working satellite does not shed parts."),
    ("latency_regression", "Ingestion latency",
     "Mean days from launch to appearing here."),
    ("uncatalogued_growth", "Uncatalogued objects",
     "Tracked but not yet catalogued. A jump can be the first visible "
     "sign of a break-up, before fragments are designated."),
    ("new_launch", "New launches", ""),
    ("ingestion_change", "Ingestion changes",
     "Old launches whose pieces arrived together — what we fetch "
     "changed, not what is in orbit."),
    ("deployment", "Deployment",
     "Payloads still being catalogued from a recent launch."),
    ("newly_visible", "Newly visible", ""),
]


def is_newsworthy(rows) -> bool:
    """
    Is there anything here a person needs to see?

    Yes if any event is notable, or if anything happened that is not one
    of the daily health metrics. No otherwise — which is the case on a
    day when nothing launched and nothing broke up.
    """
    for r in rows:
        event_type, notable = r[0], r[6]
        if notable or event_type not in ROUTINE_TYPES:
            return True
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since-hours", type=int, default=24,
                    help="Report events first detected within this many "
                         "hours (default 24)")
    ap.add_argument("--out", metavar="FILE",
                    help="Write the digest here as well as to stdout")
    ap.add_argument("--only-newsworthy", action="store_true",
                    help="Write nothing to --out unless something other "
                         "than the daily health metrics happened. Use this "
                         "when the output triggers a notification.")
    args = ap.parse_args(argv)

    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT event_type, launch_key, launch_date, object_count,
                   object_types, first_seen, notable, details, detected_at
              FROM catalog_events
             WHERE detected_at >= now() - make_interval(hours => :h)
             ORDER BY notable DESC, object_count DESC
        """), {"h": args.since_hours}).fetchall()

    if not rows:
        # Printed rather than silent so a workflow log shows the check ran
        # and found nothing, which is different from the step not running.
        print(f"No new catalogue events in the last {args.since_hours}h.")
        if args.out:
            open(args.out, "w", encoding="utf-8").close()
        return 0

    if args.only_newsworthy and not is_newsworthy(rows):
        routine = ", ".join(sorted({r[0] for r in rows}))
        print(f"{len(rows)} new event(s) in the last {args.since_hours}h, "
              f"all routine ({routine}). Nothing to report.")
        if args.out:
            open(args.out, "w", encoding="utf-8").close()
        return 0

    by_type: dict = {}
    for r in rows:
        by_type.setdefault(r[0], []).append(r)

    notable = [r for r in rows if r[6]]
    lines = [
        f"{len(rows)} new catalogue event(s) in the last "
        f"{args.since_hours}h, {len(notable)} notable.",
        "",
    ]

    for key, title, blurb in SECTIONS:
        items = by_type.pop(key, [])
        if not items:
            continue
        total = sum(i[3] or 0 for i in items)
        lines.append(f"### {title} — {len(items)} event(s), "
                     f"{total:,} object(s)")
        if blurb:
            lines.append("")
            lines.append(f"_{blurb}_")
        lines.append("")

        if key in ("latency_regression", "uncatalogued_growth"):
            for _, _, _, count, _, _, note, details, _ in items:
                d = details or {}
                if key == "latency_regression":
                    # object_count here is OBJECTS, not launches. "245
                    # launches in 30 days" would read as roughly eight a
                    # day, which is off by an order of magnitude.
                    lines.append(
                        f"- mean **{d.get('mean_lag_days')} days** across "
                        f"{count:,} objects from launches in the last "
                        f"{d.get('window_days')} days "
                        f"(alert above {d.get('threshold_days')})"
                        + ("  ⚠️" if note else ""))
                else:
                    ch = d.get("change")
                    prev = d.get("previous")
                    lines.append(
                        f"- **{count:,}** uncatalogued"
                        + (f" ({ch:+,} since {prev:,})" if prev is not None
                           else " (no prior count)")
                        + ("  ⚠️" if note else ""))
            lines.append("")
            continue

        lines.append("| Launch | Launched | Objects | Types | First seen |")
        lines.append("|---|---|---|---|---|")
        for _, lk, ld, count, types, seen, note, _, _ in items:
            flag = " ⚠️" if note and key == "fragmentation" else ""
            lines.append(
                f"| `{lk or '—'}`{flag} | {ld or '—'} | {count:,} | "
                f"{types or '—'} | {seen or '—'} |")
        lines.append("")

    for key, items in by_type.items():          # anything unmapped
        lines.append(f"### {key} — {len(items)} event(s)")
        lines.append("")

    digest = "\n".join(lines).rstrip() + "\n"
    print(digest)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
