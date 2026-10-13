"""
The SEC's SIC codes that put a filer in scope for this project.

One definition, importable by anything, depending on nothing.

Why it lives here rather than in src/funding/sec_edgar.py
--------------------------------------------------------
It started there. On 2026-10-14 the feed watcher needed it too, to tell
a space filing from the 200 Morgan Stanley prospectuses that share a
window with it, and `sec_edgar` imports `src.db.writer` and `sqlalchemy`
at module level. Importing it from `src/feeds/watch.py` would have
dragged a database engine factory into a module whose whole contract is
that it does not touch the database.

The other option was a second copy of the dict, which is the AD-112
failure with the serial numbers filed off: two copies of an identifier
list, drifting, one of them wrong, and nothing to notice. So the
constant moved to a module with no imports at all, and both callers
read the same nine codes.

Provenance
----------
Taken VERBATIM from the SEC's own Division of Corporation Finance SIC
list, read at source 2026-10-12 - the list EDGAR itself assigns from.

The first version of this constant carried "3761" and "3669", neither
of which exists in that list. The real code for the obvious one is
**3760**. A discovery query filtered on 3761 would have returned zero
rows and read as an empty universe rather than as a typo, which is the
AD-105 shape: a tool reporting its own defect as a fact about the
world. Each code therefore carries the SEC's own office description, so
a reviewer can check it against the list without a second lookup, and
`tests/test_api_compliance.py` asserts 3760 present and 3761/3669
absent.

How it may and may not be used
------------------------------
In `sec_edgar.py` it is REPORTED, never used to filter: a filer is in
the seed because a person put it there with a written basis, and the
SIC only ever adds a line to a report.

In `feeds/watch.py` it DOES filter - but a digest, not a claim. Sorting
a feed's items into "worth reading" and "a count" changes what a human
is shown; it does not decide what is true, and the out-of-scope items
are always still counted out loud so the filter cannot hide its own
effect.
"""
from __future__ import annotations

#: code -> the SEC's own office description for it.
SPACE_SIC = {
    "3760": "Guided missiles & space vehicles & parts",
    "3720": "Aircraft & parts",
    "3721": "Aircraft",
    "3724": "Aircraft engines & engine parts",
    "3728": "Aircraft parts & auxiliary equipment, NEC",
    "3812": "Search, detection, navigation, guidance, aeronautical systems",
    "3663": "Radio & TV broadcasting & communications equipment",
    "4812": "Radiotelephone communications",
    "4899": "Communications services, NEC",
}
