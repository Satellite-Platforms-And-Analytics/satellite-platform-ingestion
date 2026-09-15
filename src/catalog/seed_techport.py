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

ANSWERED 2026-09-15: `organizationId` is present on 50 of 50 sampled
projects, alongside organizationName, organizationType, city,
stateTerritory, country and organizationRole. So the id exists and is
the upsert key.

`016_research_org_key.sql` is the consequence: `name_norm` was UNIQUE
only because it was going to be the key, and leaving it so would reject
two TechPort organisation records whose names normalise alike - not as a
bad row, but as an IntegrityError partway through a ten-hour import, for
a reason that reads like corruption and is not. It now carries a UNIQUE
index only among rows that have no id, which is exactly where it is
still the key.

That swap cost nothing because the survey ran BEFORE the import and the
table held zero rows. That is the whole argument for refusing to guess.

DELIBERATELY NOT IMPORTED YET
=============================
leadOrganization also carries `city`, `stateTerritory`, `country` and
`organizationRole`, and 015 has no columns for them. They are not added
today, because backfilling them later is FREE: every detail is kept in
data/cache/techport, so a column added in a month is filled by a re-run
that makes no requests at all. A schema change that can be deferred at
zero future cost should be.

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
import re
import sys
from datetime import date, datetime

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
    # REPORT THE TWO RULES SEPARATELY. THIS WAS WRONG ON THE FIRST RUN.
    #
    # The first version took match_org(...)[0] and discarded `how`,
    # printing one combined rate - and then compared it against the 3%
    # that 09-14 measured for the STRICT rule alone. The comparison read
    # as a fivefold improvement. It was the same data: 1 strict + 5
    # agency-prefix, which 09-14 also reported as 17% combined.
    #
    # Merging them is precisely what AD-085 exists to prevent, and
    # `match_org` returns `how` precisely so it cannot happen. It was
    # thrown away one commit after a test was written asserting the label
    # matters. The information that answers the question was present and
    # discarded - the same shape as the response body, the taxonomy code
    # and detail_shape before it.
    by_how: collections.Counter = collections.Counter()
    for nm in org_names:
        _code, how = match_org(nm, strict_idx, relaxed_idx)
        by_how[how] += 1
    n_names = max(1, len(org_names))
    strict_n = by_how.get("strict", 0)
    loose_n = by_how.get("agency_prefix_stripped", 0)
    print(f"\n  AGAINST THE DATABASE")
    print(f"    research_projects present  : {already:,}")
    print(f"    STRICT  distinct performers: {strict_n}/{len(org_names)} "
          f"({100*strict_n/n_names:.0f}%)")
    print(f"    +agency-prefix stripped    : +{loose_n} -> "
          f"{strict_n+loose_n} ({100*(strict_n+loose_n)/n_names:.0f}%)")
    print(f"    unmatched                  : {by_how.get(None, 0)}")
    print(f"    (09-14 measured 3% strict, 17% combined, on a 50-project")
    print(f"     sample. Compare like with like: the combined number is")
    print(f"     mostly NASA field centres and buys almost no companies.)")

    print(f"\n  requests used: {c.n}   served from cache: {from_cache}")
    print(f"  quota remaining: {c.remaining}"
          + (f" of {c.limit}" if c.limit else ""))
    print("\n  Nothing was written.")
    return 0


#: 015's CHECK, restated here so a bad node is REPORTED and skipped
#: rather than aborting a ten-hour import. The constraint is still the
#: authority; this is what keeps one malformed node from costing the
#: other 19,689 projects.
#:
#: `X` IS A REAL SEGMENT, NOT JUNK. The first 500-project import refused
#: five nodes - TX08.X "Other Sensors and Instruments", TX11.X "Other
#: Software...", TX14.X "Other Thermal Management Systems". Those are
#: NASA's own "Other" buckets within a top-level area, and the original
#: digits-only pattern was throwing away legitimate taxonomy.
#:
#: Measured across 548 cached details: 518 strict, 5 with `.X`, and
#: nothing else at all. So the relaxation is to exactly what the source
#: uses, not to whatever might turn up. `tx_top` is unaffected -
#: split_part('TX14.X', '.', 1) is still TX14.
TX_CODE = re.compile(r"^TX[0-9]{2}(\.([0-9]+|X))*$")


def _date(v):
    """
    A date, or None - and the caller counts the Nones.

    TechPort's date formats are not documented anywhere this project has
    read, and the listing returns things like "2026-9-10" (no zero
    padding), which `date.fromisoformat` rejects on Python < 3.11. So
    parsing is tolerant and FAILURES ARE COUNTED, because a silent NULL
    is how a column quietly becomes empty while the import reports
    success - the failure mode this project has now catalogued five
    times under "an error path that destroys the error".
    """
    if not v:
        return None
    text_v = str(v).strip()[:10]
    # TECHPORT USES TWO DIFFERENT DATE FORMATS IN ONE RECORD.
    #
    # startDate and endDate are ISO ("2018-04-23"). lastUpdated is US
    # short form with a two-digit year ("01/27/25"). The first import of
    # 500 projects reported 500 unparseable dates - exactly one per
    # project - which is what sent anyone looking at the cache instead
    # of guessing.
    #
    # The MM/DD order is established, not assumed: "01/27/25" has a 27
    # in the second position, and 27 is not a month.
    #
    # %y maps 00-68 to 2000-2068. TechPort projects begin in the 2000s,
    # so the window is not a problem today and is written down for the
    # day it is.
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text_v, fmt).date()
        except ValueError:
            continue
    parts = text_v.split("-")
    if len(parts) == 3 and all(x.isdigit() for x in parts):
        try:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            return None
    return None


def parse_project(detail: dict) -> "dict | None":
    """One TechPort detail -> the rows 015 wants. Pure: no IO."""
    p = detail.get("project", detail) if isinstance(detail, dict) else {}
    pid = p.get("projectId")
    if pid is None or not p.get("title"):
        return None

    name, id_key, id_value, shape = read_lead(p)

    trl = p.get("trlCurrent")
    trl = int(trl) if isinstance(trl, (int, float)) and 1 <= trl <= 9 else None

    nodes, bad_nodes = [], []
    tx = p.get("primaryTaxonomyNodes") or p.get("primaryTx")
    for node in (tx if isinstance(tx, list) else ([tx] if tx else [])):
        if isinstance(node, dict):
            code = (node.get("code") or node.get("taxonomyNumber")
                    or node.get("number") or "")
            title = node.get("title") or ""
        else:
            code, title = "", str(node)
        code = str(code).strip()
        title = str(title).strip()
        if code and title and TX_CODE.match(code):
            nodes.append({"tx_code": code, "tx_title": title})
        elif code or title:
            bad_nodes.append({"code": code, "title": title})

    return {
        "techport_id": int(pid),
        "title": str(p.get("title")).strip(),
        "status": (p.get("status") or None),
        "start_date": _date(p.get("startDate")),
        "end_date": _date(p.get("endDate")),
        "trl_current": trl,
        "last_updated": _date(p.get("lastUpdated")),
        "org": ({"techport_org_id": int(id_value) if id_value is not None
                 else None,
                 "name": str(name).strip(),
                 "name_norm": norm(name),
                 "org_type": (p.get("leadOrganization") or {}).get(
                     "organizationType") if shape == "dict" else None}
                if name else None),
        "nodes": nodes,
        "bad_nodes": bad_nodes,
        "date_misses": sum(
            1 for k in ("startDate", "endDate", "lastUpdated")
            if p.get(k) and _date(p.get(k)) is None),
    }


def apply(c: "Client", ids: list, limit: int, use_cache: bool,
          refresh: bool = False) -> int:
    engine = get_engine()
    with engine.connect() as conn:
        done = set(conn.execute(text(
            "SELECT techport_id FROM research_projects")).scalars())
        strict_idx, relaxed_idx = load_org_index(conn)

    if refresh:
        # RE-PROCESS WHAT IS ALREADY THERE, FOR FREE.
        #
        # Every detail is kept in data/cache/techport, so a parser fix
        # can be applied to rows already imported at a cost of zero
        # requests. That is what made the MM/DD/YY discovery cheap to
        # act on: 500 rows with a NULL last_updated became 500 rows with
        # a date, without touching the API.
        todo = [i for i in ids if i in done][:max(0, limit)]
    else:
        todo = [i for i in ids if i not in done][:max(0, limit)]
    print(f"  projects in listing          : {len(ids):,}")
    print(f"  already imported             : {len(done):,}")
    print(f"  this run                     : {len(todo):,} "
          f"(--limit {limit}"
          + (", --refresh: re-parsing rows already written" if refresh
             else "") + ")")
    if not todo:
        print("\n  Nothing to do. Every project in the listing is "
              "already imported.")
        return 0

    rows, orgs, from_cache = [], {}, 0
    bad_nodes, date_misses, unparseable = [], 0, 0
    for n, pid in enumerate(todo, 1):
        d, hit = cached_detail(c, pid, use_cache=use_cache)
        from_cache += hit
        r = parse_project(d)
        if r is None:
            unparseable += 1
            continue
        date_misses += r["date_misses"]
        for b in r["bad_nodes"]:
            bad_nodes.append((r["techport_id"], b))
        if r["org"]:
            o = r["org"]
            key = o["techport_org_id"] or ("name:" + o["name_norm"])
            if key not in orgs:
                code, how = match_org(o["name"], strict_idx, relaxed_idx)
                o["organization_code"] = code
                o["match_method"] = how
                o["source_confidence"] = (
                    1.0 if how == "strict"
                    else 0.85 if how else None)
                orgs[key] = o
            r["org_key"] = key
        rows.append(r)
        if n % 50 == 0:
            print(f"    fetched {n}/{len(todo)}   quota {c.remaining}   "
                  f"cached {from_cache}")

    print(f"\n  parsed {len(rows):,} projects, {len(orgs):,} distinct "
          f"performers")

    # AN EMPTY BATCH IS NOT AN ERROR, AND SQLALCHEMY DISAGREES.
    #
    # executemany with an empty parameter list raises "A value is
    # required for bind parameter 'techport_id'" rather than doing
    # nothing. That turns a batch in which every project failed to parse
    # into a crash - and because an unparseable project is never written,
    # it is selected again on the next run, and crashes again. A single
    # malformed project at the end of the listing would make the import
    # unable to finish, permanently, at 99%.
    #
    # Found by a smoke test whose third run had exactly one project left
    # and that project unparseable. It is not a hypothetical shape: the
    # last batch of a 19,690-project import is precisely where the
    # leftovers collect.
    if not rows:
        print(f"\n  Nothing to write: all {len(todo)} projects in this "
              f"batch failed to parse.")
        print(f"  They are NOT marked done, so they will be retried - "
              f"which is correct for a")
        print(f"  transient fault and a loop for a permanent one. If this "
              f"repeats with the same")
        print(f"  count, the projects are malformed at the source and "
              f"need excluding by id.")
        return 1

    with engine.begin() as conn:
        # Performers with an id: upsert on it. Without: on name_norm,
        # against 016's partial unique index.
        with_id = [o for o in orgs.values() if o["techport_org_id"]]
        no_id = [o for o in orgs.values() if not o["techport_org_id"]]
        if with_id:
            conn.execute(text("""
                INSERT INTO research_organizations
                    (name, name_norm, org_type, techport_org_id,
                     organization_code, match_method, source_confidence,
                     matched_at)
                VALUES (:name, :name_norm, :org_type, :techport_org_id,
                        :organization_code, :match_method,
                        :source_confidence,
                        CASE WHEN :organization_code IS NULL
                             THEN NULL ELSE NOW() END)
                ON CONFLICT (techport_org_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    name_norm = EXCLUDED.name_norm,
                    org_type = COALESCE(EXCLUDED.org_type,
                                        research_organizations.org_type),
                    organization_code = EXCLUDED.organization_code,
                    match_method = EXCLUDED.match_method,
                    source_confidence = EXCLUDED.source_confidence,
                    matched_at = EXCLUDED.matched_at,
                    updated_at = NOW()
            """), with_id)
        if no_id:
            conn.execute(text("""
                INSERT INTO research_organizations
                    (name, name_norm, org_type, organization_code,
                     match_method, source_confidence, matched_at)
                VALUES (:name, :name_norm, :org_type, :organization_code,
                        :match_method, :source_confidence,
                        CASE WHEN :organization_code IS NULL
                             THEN NULL ELSE NOW() END)
                ON CONFLICT (name_norm) WHERE techport_org_id IS NULL
                DO UPDATE SET
                    name = EXCLUDED.name,
                    organization_code = EXCLUDED.organization_code,
                    match_method = EXCLUDED.match_method,
                    updated_at = NOW()
            """), no_id)

        # .all() before dict(). A CursorResult is an iterator, not a
        # mapping, and dict() of one raises rather than returning an
        # empty dict - so this fails loudly, which is the only reason it
        # was caught before an import rather than during one.
        by_id = dict(conn.execute(text(
            "SELECT techport_org_id, id FROM research_organizations "
            "WHERE techport_org_id = ANY(:v)"),
            {"v": [o["techport_org_id"] for o in with_id] or [0]}).all())
        by_norm = dict(conn.execute(text(
            "SELECT name_norm, id FROM research_organizations "
            "WHERE techport_org_id IS NULL AND name_norm = ANY(:v)"),
            {"v": [o["name_norm"] for o in no_id] or [""]}).all())

        for r in rows:
            key = r.get("org_key")
            if key is None:
                r["lead_org_id"] = None
            elif isinstance(key, int):
                r["lead_org_id"] = by_id.get(key)
            else:
                r["lead_org_id"] = by_norm.get(key[5:])

        conn.execute(text("""
            INSERT INTO research_projects
                (techport_id, title, status, start_date, end_date,
                 trl_current, lead_org_id, last_updated, fetched_at)
            VALUES (:techport_id, :title, :status, :start_date, :end_date,
                    :trl_current, :lead_org_id, :last_updated, NOW())
            ON CONFLICT (techport_id) DO UPDATE SET
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                start_date = EXCLUDED.start_date,
                end_date = EXCLUDED.end_date,
                trl_current = EXCLUDED.trl_current,
                lead_org_id = EXCLUDED.lead_org_id,
                last_updated = EXCLUDED.last_updated,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = NOW()
        """), [{k: r[k] for k in (
            "techport_id", "title", "status", "start_date", "end_date",
            "trl_current", "lead_org_id", "last_updated")} for r in rows])

        tax = [{"techport_id": r["techport_id"], **nd}
               for r in rows for nd in r["nodes"]]
        if tax:
            conn.execute(text("""
                INSERT INTO research_project_taxonomy
                    (techport_id, tx_code, tx_title)
                VALUES (:techport_id, :tx_code, :tx_title)
                ON CONFLICT (techport_id, tx_code) DO UPDATE SET
                    tx_title = EXCLUDED.tx_title
            """), tax)

    with engine.connect() as conn:
        totals = conn.execute(text("""
            SELECT (SELECT count(*) FROM research_projects),
                   (SELECT count(*) FROM research_organizations),
                   (SELECT count(*) FROM research_project_taxonomy),
                   (SELECT count(*) FROM research_organizations
                     WHERE organization_code IS NOT NULL)
        """)).one()

    print(f"\n  WRITTEN")
    print(f"    research_projects          : {totals[0]:,} total")
    print(f"    research_organizations     : {totals[1]:,} total "
          f"({totals[3]:,} linked to an operator)")
    print(f"    research_project_taxonomy  : {totals[2]:,} total")

    # ── WHAT WAS NOT WRITTEN, NAMED ──────────────────────────────────
    #
    # An import that reports only what it wrote is an import that hides
    # what it dropped.
    print(f"\n  SKIPPED OR MISSING")
    print(f"    details that would not parse : {unparseable}")
    print(f"    dates that would not parse   : {date_misses}")
    print(f"    taxonomy nodes refused       : {len(bad_nodes)}")
    for pid, b in bad_nodes[:5]:
        print(f"      {pid}  code={b['code'][:24]!r} "
              f"title={b['title'][:34]!r}")
    if bad_nodes:
        print(f"      (a node whose code fails 015's CHECK is skipped and "
              f"listed, not written\n"
              f"       as NULL and not allowed to abort the run)")

    remaining = len(ids) - totals[0]
    print(f"\n  requests used: {c.n}   served from cache: {from_cache}")
    print(f"  quota remaining: {c.remaining}"
          + (f" of {c.limit}" if c.limit else ""))
    if remaining > 0:
        print(f"\n  {remaining:,} projects still to import. Re-run; the "
              f"rows already written are the watermark.")
    else:
        print(f"\n  Every project in the listing is imported.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--survey", action="store_true",
                    help="Measure, write nothing. The default.")
    ap.add_argument("--apply", action="store_true",
                    help="Import. Writes at most --limit projects.")
    ap.add_argument("--sample", type=int, default=50,
                    help="Projects to inspect in a survey (default 50).")
    ap.add_argument("--limit", type=int, default=500,
                    help="Projects per --apply run (default 500).")
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-parse projects ALREADY imported instead of "
                         "fetching new ones. Free when the cache is "
                         "warm; how a parser fix reaches rows already "
                         "written.")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    c = Client(_key())
    print(f"TechPort R&D import - "
          f"{'APPLY' if args.apply else 'SURVEY, read-only'}\n")
    print(f"  listing projects updated since {args.since} ...")
    ids = project_ids(c, args.since)
    if args.apply:
        return apply(c, ids, args.limit, use_cache=not args.no_cache,
                     refresh=args.refresh)
    return survey(c, ids, args.sample, use_cache=not args.no_cache)




if __name__ == "__main__":
    sys.exit(main())
