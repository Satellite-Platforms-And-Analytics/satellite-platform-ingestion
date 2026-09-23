#!/usr/bin/env python
"""Is the TechPort import actually running? Answer from the log, not the task.

    python check_import.py                # health of the import
    python check_import.py --max-age 6    # allow a longer gap
    python check_import.py --self-test    # prove the check can go red

WHY THIS EXISTS
---------------
On 2026-09-19 the scheduled task was installed, run by hand once, and
confirmed working by reading the log one time: exit=0, 3,000 -> 6,000
projects. It was then called autonomous.

It ran four more times and stopped. For three days `Get-ScheduledTask`
reported it Enabled and Ready, `LastRunTime` advanced hourly, and
`NumberOfMissedRuns` was 0 -- while the log sat frozen and not one
project was imported. Everything that reported on the task reported
health. Only the log knew.

    A job that reports success once has been observed succeeding once.

So this reads the artefact the work leaves behind, and nothing else.

THE TRAP THIS AVOIDS
--------------------
`exit=1` is NOT a failure here. The importer stops itself when the key's
remaining quota falls below 200 and exits non-zero, which is correct
behaviour and the whole reason the account is safe. A checker keying on
the exit code would cry wolf every time the guard worked, and would be
switched off within a week. It reads the reason instead.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOG = Path(r"D:\Databases\satellite\archive\techport_import.log")

BANNER = re.compile(
    r"^=+ \w{3} (\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?) =+\s*$")
EXIT = re.compile(r"^exit=(-?\d+)\s*$")
QUOTA_STOP = re.compile(r"^stopping: (\d+) requests left on this key")
IMPORTED = re.compile(r"already imported\s*:\s*([\d,]+)")
REMAINING = re.compile(r"([\d,]+) projects still to import")
# The importer prints the listing size at the top of every run and the
# remaining count only at the end. A run cut short by the quota guard has
# the first and not the second -- which is precisely the run you are most
# likely to be looking at -- so the total is taken from either.
LISTING = re.compile(r"projects in listing\s*:\s*([\d,]+)")
DONE = re.compile(r"Every project in the listing is imported")
PY_MISSING = re.compile(r"ERROR: python not found")


def parse(text: str) -> list[dict]:
    """Split the log into runs. A run starts at a banner and ends at the
    next one, so a run that never wrote anything is still a run -- which
    is exactly the case worth seeing."""
    runs: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = BANNER.match(line)
        if m:
            stamp = m.group(1)
            fmt = "%m/%d/%Y %H:%M:%S.%f" if "." in stamp else "%m/%d/%Y %H:%M:%S"
            cur = {"started": datetime.strptime(stamp, fmt), "exit": None,
                   "quota_stop": None, "imported": None, "remaining": None,
                   "done": False, "py_missing": False, "listing": None,
                   "lines": 0}
            runs.append(cur)
            continue
        if cur is None:
            continue
        cur["lines"] += 1
        if (m := EXIT.match(line)):
            cur["exit"] = int(m.group(1))
        elif (m := QUOTA_STOP.match(line.strip())):
            cur["quota_stop"] = int(m.group(1))
        elif (m := IMPORTED.search(line)):
            cur["imported"] = int(m.group(1).replace(",", ""))
        elif (m := REMAINING.search(line)):
            cur["remaining"] = int(m.group(1).replace(",", ""))
        elif (m := LISTING.search(line)):
            cur["listing"] = int(m.group(1).replace(",", ""))
        elif DONE.search(line):
            cur["done"] = True
        elif PY_MISSING.search(line):
            cur["py_missing"] = True
    return runs


def health(runs: list[dict], now: datetime, max_age_h: float,
           in_flight_min: float = 35.0):
    """Return (ok, headline, notes). Never judges by exit code alone."""
    notes: list[str] = []
    if not runs:
        return False, "the log has no runs in it at all", notes

    last = runs[-1]
    age = now - last["started"]
    age_h = age.total_seconds() / 3600

    if any(r["done"] for r in runs):
        return True, "import COMPLETE — remove the scheduled task", [
            'schtasks /Delete /TN "TechPort import" /F']

    if last["py_missing"]:
        return False, "python was not found at the configured path", [
            "the task ran but could not start the interpreter"]

    # A run that produced a banner and nothing else either never really
    # ran, or is running RIGHT NOW. The cmd appends python's output as it
    # flushes, so a fresh run legitimately shows a banner alone for
    # minutes. Reported as a failure on 2026-09-23 against a healthy run
    # 90 seconds old -- the exact cry-wolf this file's docstring warns
    # about. Age is what separates the two.
    # 35 minutes, from measurement rather than taste: the 09-19 23:20 run
    # spent 1,497 requests and the next banner is at 23:44 -- 24 minutes
    # for a full batch. The first value here was 10, which is SHORTER THAN
    # A NORMAL RUN, so it would have called a healthy import dead at
    # minute 11. Still well inside the hourly cadence, so a genuinely dead
    # run is caught before the next trigger fires.
    body_less = last["lines"] <= 1
    if body_less and age.total_seconds() / 60 <= in_flight_min:
        notes.append("running now — output is buffered until python flushes; "
                     "re-check in a few minutes")
        return True, (f"started {last['started']:%H:%M} "
                      f"({fmt_age(age)} ago), in flight"), notes
    if body_less:
        return False, (f"last run {last['started']:%Y-%m-%d %H:%M} "
                       f"({fmt_age(age)} ago) produced NO output"), [
            "a banner and nothing else: the task started and the script "
            "died before writing anything"]

    stale = age_h > max_age_h
    headline = (f"last run {last['started']:%Y-%m-%d %H:%M} "
                f"({fmt_age(age)} ago)")

    if last["quota_stop"] is not None:
        notes.append(
            f"stopped at the quota floor with {last['quota_stop']} requests "
            f"left — correct behaviour, not a failure")
    elif last["exit"] not in (0, None):
        notes.append(f"exit={last['exit']} for a reason other than the "
                     f"quota guard — read the log")

    if last["imported"] is not None:
        done_n = last["imported"]
        if last["remaining"] is not None:
            total = done_n + last["remaining"]
        else:
            total = last["listing"]
        if total:
            left = total - done_n
            pct = 100.0 * done_n / total
            notes.append(f"{done_n:,} of {total:,} imported ({pct:.0f}%), "
                         f"{left:,} to go")

    if stale:
        notes.append(f"NOTHING has run for {fmt_age(age)}. The task can look "
                     f"Enabled and Ready while never starting its action.")
    return (not stale), headline, notes


def fmt_age(d: timedelta) -> str:
    h = d.total_seconds() / 3600
    if h < 1:
        return f"{int(d.total_seconds() // 60)} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"


FIXTURE_RUN = """
======== Sat 09/19/2026 22:20:02.46 ========
TechPort R&D import - APPLY
  already imported             : 6,000
  parsed 1,500 projects
  1,500 projects still to import. Re-run; the rows already written are the watermark.
exit=0
"""

FIXTURE_QUOTA = """
======== Sat 09/19/2026 23:44:18.33 ========
stopping: 199 requests left on this key, floor is 200. Nothing was written; re-run in an hour.
  already imported             : 7,500
  12,184 projects still to import.
exit=1
"""

FIXTURE_CUT = """
======== Sat 09/19/2026 23:44:18.33 ========
stopping: 199 requests left on this key, floor is 200. Nothing was written; re-run in an hour.
  projects in listing          : 19,684
  already imported             : 7,500
    fetched 250/1500   quota 200   cached 0
exit=1
"""

FIXTURE_DEAD = """
======== Sat 09/19/2026 23:44:18.33 ========
"""


def self_test() -> int:
    f = []

    runs = parse(FIXTURE_RUN)
    if len(runs) != 1 or runs[0]["exit"] != 0:
        f.append(f"failed to parse a normal run: {runs}")

    # 40 minutes old, limit 2h -> healthy. Both directions of the window
    # are asserted, because a checker that can only go red is as useless
    # as one that can only go green.
    ok, _, _ = health(runs, datetime(2026, 9, 19, 23, 0), 2)
    if not ok:
        f.append("a 40-minute-old successful run was reported stale")
    ok, _, _ = health(runs, datetime(2026, 9, 20, 0, 30), 2)
    if ok:
        f.append("a run 2h10m old passed a 2h freshness limit")

    # the trap: exit=1 at the quota floor is CORRECT and must stay green
    runs = parse(FIXTURE_QUOTA)
    if runs[0]["quota_stop"] != 199:
        f.append(f"quota stop not detected: {runs[0]}")
    ok, _, notes = health(runs, datetime(2026, 9, 19, 23, 50), 2)
    if not ok:
        f.append("a recent quota stop was reported unhealthy — this is the "
                 "false alarm that gets checkers switched off")
    if not any("correct behaviour" in n for n in notes):
        f.append("quota stop was not explained as correct")

    # staleness must go red — the actual 09-20..09-23 failure
    ok, _, notes = health(parse(FIXTURE_QUOTA), datetime(2026, 9, 23, 0, 0), 2)
    if ok:
        f.append("a log 3 days stale was reported healthy — this is the "
                 "whole reason the script exists")
    if not any("NOTHING has run" in n for n in notes):
        f.append("staleness was not named in the notes")

    # a run cut short by the quota guard has no "still to import" line;
    # the progress must still come out, from the listing size instead
    _, _, notes = health(parse(FIXTURE_CUT), datetime(2026, 9, 19, 23, 50), 2)
    if not any("7,500 of 19,684" in n for n in notes):
        f.append(f"progress lost when the run was cut short: {notes}")

    # A full batch takes ~24 minutes and buffers its output, so a
    # body-less run must stay green well past that. 6 and 20 minutes are
    # both alive; 45 is not.
    for mins in (6, 20):
        when = datetime(2026, 9, 19, 23, 44) + timedelta(minutes=mins)
        ok, head, _ = health(parse(FIXTURE_DEAD), when, 2)
        if not ok or "in flight" not in head:
            f.append(f"a run {mins} minutes old was called dead — a full "
                     f"batch takes about 24 minutes")

    ok, head, _ = health(parse(FIXTURE_DEAD),
                         datetime(2026, 9, 20, 0, 29), 2)
    if ok or "NO output" not in head:
        f.append("a body-less run 45 minutes old was not reported as dead")

    # progress arithmetic
    _, _, notes = health(parse(FIXTURE_QUOTA), datetime(2026, 9, 19, 23, 50), 2)
    if not any("7,500 of 19,684" in n for n in notes):
        f.append(f"progress line wrong: {notes}")

    for x in f:
        print(f"  FAIL  {x}")
    print(f"\nself-test: {'FAILED' if f else 'passed'} "
          f"({len(f)} failure{'' if len(f) == 1 else 's'})")
    return 1 if f else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=str(LOG))
    ap.add_argument("--max-age", type=float, default=2.0,
                    help="hours since the last run before this goes red")
    ap.add_argument("--in-flight", type=float, default=35.0,
                    help="minutes a run may show no output before it is "
                         "called dead (a full batch takes about 24)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    path = Path(args.log)
    if not path.is_file():
        print(f"NO LOG at {path}")
        print("The task has never produced output. That is not 'not yet' — "
              "a run that starts writes a banner before it does anything.")
        return 1

    runs = parse(path.read_text(encoding="utf-8", errors="replace"))
    ok, headline, notes = health(runs, datetime.now(), args.max_age,
                                 args.in_flight)

    print(f"{'OK  ' if ok else 'STALE'}  {headline}")
    for n in notes:
        print(f"        {n}")
    if not ok:
        print("\n  Check, in this order:")
        print("    Get-ScheduledTask -TaskName 'TechPort import' | "
              "Get-ScheduledTaskInfo")
        print("    Get-WinEvent -FilterHashtable "
              "@{LogName='Microsoft-Windows-TaskScheduler/Operational'} "
              "-MaxEvents 40 | ? Message -match TechPort | fl TimeCreated, Message")
        print("  LastTaskResult 2147946720 (0x800710E0) means the action was "
              "refused by a condition, not that it ran and failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
