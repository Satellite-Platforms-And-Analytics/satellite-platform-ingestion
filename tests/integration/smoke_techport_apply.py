#!/usr/bin/env python
"""
Drive the whole TechPort apply path against a throwaway PostgreSQL.

NOT collected by pytest - no `test_` prefix, on purpose. It needs a
database with 015 and 016 applied, and it writes rows. CI has neither,
and a suite that is routinely red is a suite nobody reads.

WHAT IT IS FOR, AND WHAT IT ALREADY FOUND
=========================================
The unit tests cover `parse_project` and `_date`, which are pure. They
cannot cover the three things that actually break an importer: the SQL,
the transaction, and the batching. This runs those, with synthetic
details and a fake client, so no request is made and the result is
deterministic.

Run on 2026-09-15 before the apply path was committed. It found two
defects the unit tests could not have:

  1. `dict(conn.execute(...))` - a CursorResult is an iterator, not a
     mapping, and SQLAlchemy 2.0 raises on it. Every lead_org_id would
     have been NULL if it had returned an empty dict instead; it failed
     loudly, which is the only reason this was cheap.

  2. AN ALL-UNPARSEABLE BATCH CRASHED. executemany with an empty
     parameter list raises rather than doing nothing, and an unparseable
     project is never written, so it is selected again next run and
     crashes again. One malformed project at the end of the listing
     would have made a 19,690-project import unable to finish,
     permanently, at 99% - and the last batch is exactly where leftovers
     collect.

The fixtures below are chosen to exercise what the schema argues about:

  project 1  a clean row whose lead org matches only after the agency
             prefix is stripped, so match_method must record which rule
  projects 2+3  two DIFFERENT TechPort org ids whose names normalise
             alike. 015 would have rejected the second with a UNIQUE
             violation; 016 is what lets both exist
  project 4  a taxonomy node with a TITLE in the code field (015's
             CHECK), an unparseable date, and no lead organisation
  project 5  no projectId: unparseable, must be counted and must not
             become a blank row

USAGE
=====
    initdb / start a scratch postgres, then apply 015 and 016, then:

    DATABASE_URL=postgresql://... python tests/integration/smoke_techport_apply.py

Expect: run 1 writes 3, run 2 writes 1 and lists what it refused, run 3
writes nothing and explains why rather than raising.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("NASA_API_KEY", "x" * 40)

from src.catalog import seed_techport as st                   # noqa: E402

DETAILS = {
    1: {"project": {
        "projectId": 1, "title": "Quantum Computer", "status": "Active",
        "trlCurrent": 7, "startDate": "2024-01-15",
        "endDate": "2026-12-31", "lastUpdated": "2026-9-10",
        "leadOrganization": {
            "organizationName": "NASA Goddard Space Flight Center",
            "organizationId": 42, "organizationType": "NASA_CENTER"},
        "primaryTaxonomyNodes": [
            {"code": "TX11.6.4", "title": "Quantum Computer"}]}},
    2: {"project": {
        "projectId": 2, "title": "No TRL, no taxonomy",
        "lastUpdated": "2026-08-01",
        "leadOrganization": {"organizationName": "Thinkorbital Inc.",
                             "organizationId": 77,
                             "organizationType": "INDUSTRY"}}},
    3: {"project": {
        "projectId": 3, "title": "Same normalised name, different id",
        "trlCurrent": 3, "lastUpdated": "2026-07-01",
        "leadOrganization": {"organizationName": "Thinkorbital, Inc",
                             "organizationId": 78}}},
    4: {"project": {
        "projectId": 4, "title": "Bad taxonomy + bad dates",
        "startDate": "soon", "leadOrganization": None,
        "primaryTaxonomyNodes": [
            {"code": "Sensors and Instruments", "title": "title-as-code"},
            {"code": "TX08", "title": "Sensors and Instruments"}]}},
    5: {"project": {"title": "no projectId -> unparseable"}},
}


class FakeClient:
    """Asserts rather than requests. A smoke test that hits the network
    is a smoke test that fails for reasons unrelated to the code."""
    n = 0
    remaining = 1900
    limit = 2000

    def get(self, *a, **k):                                   # pragma: no cover
        raise AssertionError("the smoke test must make no requests")


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set. This needs a scratch database "
              "with 015 and 016 applied.")
        return 2
    st.cached_detail = lambda c, pid, use_cache=True: (DETAILS[pid], True)
    ids = list(DETAILS)
    print("=== run 1: --limit 3 ===")
    st.apply(FakeClient(), ids, 3, use_cache=True)
    print("\n=== run 2: the rest, proving the rows are the watermark ===")
    st.apply(FakeClient(), ids, 10, use_cache=True)
    print("\n=== run 3: only the unparseable one left ===")
    rc = st.apply(FakeClient(), ids, 10, use_cache=True)
    print(f"\n(run 3 returned {rc}; non-zero and NOT an exception is the "
          f"point)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
