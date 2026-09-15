"""
Import NASA TechPort into the R&D layer. Survey first; apply only once
the survey has answered what the schema deliberately left open.

WHERE THIS CAME FROM
====================
`015_research_activity.sql` replaced the planned
`organization_technologies` edge after the 2026-09-14 survey measured a
3% organisation match - not a matching defect, but two genuinely
different populations (AD-083). TechPort names who does the R&D; GCAT
names who operates the hardware. AD-084 then made R&D a tracked aspect of
technology in its own right, so the source is right and the question was
wrong.

THE QUESTION THIS SURVEY EXISTS TO ANSWER
=========================================
015 records an open question rather than guessing at it:

  > TechPort's `leadOrganization` is an object. The survey extracted
  > `organizationName` from it and nothing else, so it is NOT KNOWN
  > whether it carries a stable organisation id.

`research_organizations` therefore uses a surrogate key with a UNIQUE
normalised name, and reserves `techport_org_id` for when this is
answered. **Until it is answered, `--apply` refuses to run.** Keying an
import on a field nobody has looked at is the mistake the 2026-09-11
plan made and 09-12 corrected, and it is cheaper to refuse than to
migrate out of.

PREDICTIONS, WRITTEN 2026-09-15 BEFORE THE FIRST RUN
====================================================
    leadOrganization carries an id                  60%  likely
    distinct performers across 19,690 projects   1,500 - 4,000
    taxonomy rows per project                      1.0 - 1.5
    projects whose lead org matches `organizations`  2 - 6%

The last one is a re-prediction of a number already measured at 3% on a
50-project sample. Stated again because a full import is 390x the sample
and a rate that moves a lot between them would say the sample was not
representative - which is worth knowing either way.

WHAT AN IMPORT COSTS, AND WHY IT IS BATCHED
===========================================
19,690 projects at one detail request each, against a ceiling this key
reports as ~2,000/hour, is a ten-hour job. It is not one run.

The resume mechanism is deliberately NOT a watermark table: it is the
database plus the disk cache. `--apply --limit N` takes the next N
projects not already in `research_projects`, so the rows already written
ARE the watermark, and `data/cache/techport/` means a re-run over
already-fetched projects costs nothing. Two mechanisms that already
exist, no third one to keep correct.

RATE LIMITS (AD-060, docs/API_USAGE_POLICY.md)
==============================================
Through `api.nasa.gov` with a key, never `techport.nasa.gov` direct.
The Client in check_techport.py reads X-RateLimit-Remaining from every
response and stops at QUOTA_FLOOR with a fifth of the budget unspent.
This module reuses it rather than opening a second front door.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

from src.env import bootstrap

bootstrap()

from sqlalchemy import text                                  # noqa: E402

from src.catalog.org_match import (                          # noqa: E402
    NORM_VERSION, load_org_index, match_org, norm,
)
from src.db.writer import get_engine                         # noqa: E402

# One client, one User-Agent, one quota rule. check_techport.py owns it.
from check_techport import (                                  # noqa: E402
    Client, _key, cached_detail,
)

#: Keys we would need on `leadOrganization` for `techport_org_id` to be
#: usable. Any one of them is enough; the survey reports which.
ID_KEYS = ("organizationId", "id", "organizationID", "orgId")


def read_lead(project: "dict") -> tuple:
    """
    -> (name, id_key, id_value, shape)

    Pulled out of the survey loop so it can be tested without a network
    call, because everything this function decides is a schema decision:
    whether `techport_org_id` can be populated at all, and what
    `research_organizations` is upserted on.

    `shape` is reported rather than assumed. TechPort's leadOrganization
    is an object today; a survey that silently coped with a plain string
    would hide the day it changed - the same column-drift precedent
    `fetch_gcat_catalog()` carries a warning for.
    """
    lead = project.get("leadOrganization")
    if isinstance(lead, dict):
        name = lead.get("organizationName")
        for key in ID_KEYS:
            if lead.get(key) is not None:
                return name, key, lead[key], "dict"
        return name, None, None, "dict"
    if lead:
        return str(lead), None, None, type(lead).__name__
    return None, None, None, "absent"


def project_ids(c: "Client", since: str) -> list:
    listing = c.get("/projects", updatedSince=since)
    node = listing.get("projects") if isinstance(listing, dict) else None
    if isinstance(node, dict):
        node = node.get("projects")
    if not isinstance(node, list):
        raise SystemExit(
            f"could not find the id list in the listing response. "
            f"Top-level keys: {list(listing)[:8]}")
    out = []
    for x in node:
        pid = x.get("projectId") if isinstance(x, dict) else x
        if pid is not None:
            out.append(pid)
    return out


def survey(c: "Client", ids: list, sample: int, use_cache: bool) -> int:
    n_total = len(ids)
    stride = max(1, n_total // max(1, sample))
    take = [ids[min(n_total - 1, k * stride)] for k in range(sample)]
    print(f"  projects in listing          : {n_total:,}")
    print(f"  sampling                     : spread, {len(take)} "
          f"(stride {stride})")

    lead_shapes: collections.Counter = collections.Counter()
    id_key_hits: collections.Counter = collections.Counter()
    org_names: collections.Counter = collections.Counter()
    org_ids: dict = {}
    tx_per_project: collections.Counter = collections.Counter()
    from_cache = 0
    have_trl = 0

    for n, pid in enumerate(take, 1):
        d, hit = cached_detail(c, pid, use_cache=use_cache)
        from_cache += hit
        p = d.get("project", d) if isinstance(d, dict) else {}

        name, id_key, id_value, shape = read_lead(p)
        lead_shapes[shape] += 1
        if shape == "dict":
            for k in (p.get("leadOrganization") or {}):
                id_key_hits[k] += 1
        if id_key:
            org_ids.setdefault(norm(name), (id_key, id_value))
        if name:
            org_names[str(name).strip()] += 1

        if isinstance(p.get("trlCurrent"), (int, float)) and p["trlCurrent"]:
            have_trl += 1

        tx = p.get("primaryTaxonomyNodes") or p.get("primaryTx")
        nodes = tx if isinstance(tx, list) else ([tx] if tx else [])
        tx_per_project[len(nodes)] += 1

        if n % 10 == 0:
            print(f"    {n}/{len(take)}   quota {c.remaining}   "
                  f"cached {from_cache}")

    s = len(take) or 1

    # ── THE OPEN QUESTION ────────────────────────────────────────────
    print(f"\n  THE QUESTION 015 LEFT OPEN")
    print(f"    leadOrganization shape     : {dict(lead_shapes)}")
    if id_key_hits:
        print(f"    its keys                   :")
        for k, cnt in id_key_hits.most_common(12):
            mark = "  <-- an id" if k in ID_KEYS else ""
            print(f"      {cnt:>4}/{s}  {k}{mark}")
    found = [k for k in ID_KEYS if id_key_hits.get(k)]
    if found:
        key = found[0]
        coverage = 100 * id_key_hits[key] / s
        print(f"\n    ANSWERED: `{key}` is present on {coverage:.0f}% of "
              f"sampled projects.")
        print(f"    -> populate research_organizations.techport_org_id "
              f"from it, and")
        print(f"       upsert on it rather than on name_norm. The FK does "
              f"not change.")
        if coverage < 100:
            print(f"    -> but NOT on all of them, so name_norm stays the "
                  f"fallback key.")
    else:
        print(f"\n    ANSWERED: no id field on leadOrganization in this "
              f"sample.")
        print(f"    -> name_norm (NORM_VERSION={NORM_VERSION}) is the "
              f"upsert key, and")
        print(f"       techport_org_id stays NULL. A performer that "
              f"renames becomes a")
        print(f"       new row; that is a known limit, not a surprise.")

    # ── Sizing ───────────────────────────────────────────────────────
    tx_mean = (sum(k * v for k, v in tx_per_project.items()) / s)
    print(f"\n  WHAT A FULL IMPORT WOULD WRITE (extrapolated from {s})")
    print(f"    research_projects          : ~{n_total:,}")
    print(f"    taxonomy nodes per project : {tx_mean:.2f} "
          f"({dict(sorted(tx_per_project.items()))})")
    print(f"    research_project_taxonomy  : ~{int(n_total * tx_mean):,}")
    distinct_rate = len(org_names) / max(1, sum(org_names.values()))
    print(f"    distinct performers        : {len(org_names)} in {s} "
          f"projects -> ~{int(n_total * distinct_rate):,} (an UPPER bound:")
    print(f"                                 the rate falls as the sample "
          f"grows and names repeat)")
    print(f"    with a TRL                 : {have_trl}/{s} "
          f"({100*have_trl/s:.0f}%)")

    # ── Cost ─────────────────────────────────────────────────────────
    limit = c.limit or 1000
    hours = n_total / max(1, limit)
    print(f"\n  WHAT IT COSTS")
    print(f"    detail requests            : {n_total:,} (one per project)")
    print(f"    ceiling this key reports   : {limit:,}/hour")
    print(f"    at that ceiling            : {hours:.1f} hours, so "
          f"{max(1, round(hours)):.0f}+ runs")
    print(f"    -> --apply --limit N. Rows already in research_projects "
          f"are the watermark;")
    print(f"       data/cache/techport means a re-run over fetched "
          f"projects costs nothing.")

    # ── Against the database ─────────────────────────────────────────
    engine = get_engine()
    with engine.connect() as conn:
        strict_idx, relaxed_idx = load_org_index(conn)
        already = conn.execute(text(
            "SELECT count(*) FROM research_projects")).scalar()
    matched = sum(1 for nm in org_names
                  if match_org(nm, strict_idx, relaxed_idx)[0])
    print(f"\n  AGAINST THE DATABASE")
    print(f"    research_projects present  : {already:,}")
    print(f"    performers matching an org : {matched}/{len(org_names)} "
          f"({100*matched/max(1,len(org_names)):.0f}%)")
    print(f"    (predicted 2-6% of PROJECTS; measured 3% of distinct "
          f"names on 09-14)")

    print(f"\n  requests used: {c.n}   served from cache: {from_cache}")
    print(f"  quota remaining: {c.remaining}"
          + (f" of {c.limit}" if c.limit else ""))
    print("\n  Nothing was written.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--survey", action="store_true",
                    help="Measure, write nothing. The default.")
    ap.add_argument("--apply", action="store_true",
                    help="Import. Refuses until the survey has answered "
                         "the leadOrganization question.")
    ap.add_argument("--sample", type=int, default=50,
                    help="Projects to inspect in a survey (default 50).")
    ap.add_argument("--limit", type=int, default=500,
                    help="Projects per --apply run (default 500).")
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    if args.apply:
        # REFUSING IS THE FEATURE.
        #
        # The apply path has to decide what research_organizations is
        # keyed on, and 015 records that as unanswered. Writing it now
        # means guessing, and a guess about a primary key is the
        # expensive kind: it is not wrong until there are rows, and by
        # then it is a migration.
        print("--apply is not implemented yet, on purpose.\n")
        print("It cannot be written correctly until the survey answers "
              "whether TechPort's")
        print("leadOrganization carries a stable id - that decides "
              "whether performers are")
        print("upserted on techport_org_id or on name_norm, and 015 "
              "records it as an open")
        print("question rather than a guess.\n")
        print("Run the survey and read THE QUESTION 015 LEFT OPEN:\n")
        print("    python -m src.catalog.seed_techport --survey\n")
        print("It costs ~51 requests, and nothing after the first run "
              "if the cache is warm.")
        return 2

    c = Client(_key())
    print("TechPort R&D import - SURVEY, read-only\n")
    print(f"  listing projects updated since {args.since} ...")
    ids = project_ids(c, args.since)
    return survey(c, ids, args.sample, use_cache=not args.no_cache)


if __name__ == "__main__":
    sys.exit(main())
