"""
Survey Launch Library 2 before writing an importer for it. MVP piece 2.

    python check_launches.py

WHAT THIS SOURCE IS, AND WHY IT IS NOT LIKE THE LAST ONE
========================================================
TechPort was 19,690 records at 2,000 requests/hour: a ten-hour job that
had to be batched, resumable and watermarked. Launch Library 2 is the
opposite shape and needs none of that machinery.

    upcoming launches            363   (read from the API, 2026-09-15)
    free tier                    15 requests/HOUR
    a full pass at limit=100     4 requests

Four requests gets everything. The whole dataset fits in one page of
memory. The design problem here is not volume - it is that the budget is
133x tighter per request, so a careless loop is not slow, it is a
suspension.

THE RATE LIMIT IS PUBLISHED; THE LICENCE IS NOT
===============================================
theSpaceDevs states, on its own page: "all the API data is available at
no cost for up to 15 requests per hour."

Nothing on their site states a licence, an attribution requirement, or a
caching rule. That is the MIRROR of AD-060. TechPort's danger was an
endpoint with no published rate limit - you find the ceiling by hitting
it. Here the ceiling is published and the TERMS are the gap.

Recorded as a decision rather than left implicit:

  - Attribute theSpaceDevs wherever this data renders. An unstated
    attribution requirement is not an absent one, and the cost of
    crediting a free service is nil.
  - Cache, and treat the cache as mandatory rather than polite. At 15/hr
    a re-run without one is a meaningful fraction of the hourly budget.
  - Do not republish the dataset in bulk. Reading it for a product
    surface is plainly within "available at no cost"; mirroring it is a
    different act that nobody has given permission for.

PREDICTIONS, WRITTEN BEFORE THE FIRST FULL RUN
==============================================
    upcoming launches               363      (known, not predicted)
    net_precision of Hour or finer  40 - 70%
    distinct providers (lsp_name)   40 - 80
    providers matching `organizations`   25 - 55%   <-- the decisive one

THE LAST ONE IS THE POINT OF THE SURVEY, and it is a real test of AD-083
rather than a repeat of it. TechPort matched 3% because it names who does
the R&D while GCAT names who operates hardware - two populations. Launch
providers are a THIRD population, and the prediction is that they overlap
GCAT heavily: SpaceX, Arianespace, ULA, Roscosmos and CASC are launch
agencies, and GCAT's org table carries an 'LA' and 'LV' type precisely
for them.

If that lands high, AD-083 is a finding about TechPort specifically. If
it lands near 3%, it is a finding about name-matching in general, and the
next importer should be designed accordingly. Either answer is worth four
requests.

NET IS NOT A LAUNCH DATE
========================
`net` is "no earlier than", and it arrives with `net_precision`:

    {"id": 0, "name": "Second"}

A launch known to the second and one known only to the month are both
ISO timestamps, and storing them the same way presents a guess as a
fact - the same category error AD-061 caught when TechPort's per-project
TRL was about to be written into `technologies.trl`. The survey reports
the precision distribution first, because it decides whether the schema
needs a precision column, and the answer is almost certainly yes.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time

from src.env import bootstrap

bootstrap()

import requests                                              # noqa: E402
from sqlalchemy import text                                  # noqa: E402

from src.catalog.org_match import (                          # noqa: E402
    load_org_index, match_org,
)
from src.db.writer import get_engine                         # noqa: E402
from src.useragent import user_agent                         # noqa: E402

BASE = "https://ll.thespacedevs.com/2.2.0"

#: Their maximum page size. Fewer, larger pages is the only lever that
#: matters at 15 requests/hour.
PAGE = 100

#: Requests to spend, ever, in one run. Four covers 363 launches with one
#: to spare; the cap exists so a pagination bug costs a stopped run
#: rather than an hour of suspension.
MAX_REQUESTS = 6

#: Between requests. 15/hour is one every four minutes sustained, so a
#: burst of four is well inside the budget - but not back to back.
PAUSE_S = 2.0

CACHE = pathlib.Path(__file__).resolve().parent / "data" / "cache" / "launches"

#: The upcoming manifest moves daily; a stale one is misleading rather
#: than merely old. Short, but non-zero, because at 15/hr a re-run inside
#: the same session must not cost anything.
CACHE_TTL_S = 6 * 3600

USER_AGENT = user_agent("upcoming launch manifest", "<=15 req/hr")


def fetch(session, path: str, params: dict, n: list, use_cache: bool):
    key = CACHE / (path.strip("/").replace("/", "_") +
                   f"_{params.get('offset', 0)}.json")
    if use_cache and key.is_file():
        if time.time() - key.stat().st_mtime < CACHE_TTL_S:
            try:
                return json.loads(key.read_text(encoding="utf-8")), True
            except (OSError, ValueError):
                pass
    if n[0] >= MAX_REQUESTS:
        raise SystemExit(
            f"stopping: {MAX_REQUESTS} requests is this run's whole "
            f"budget and it is spent. The free tier is 15/hour and a "
            f"loop that does not stop itself is how an account gets "
            f"suspended. Nothing was written.")
    r = session.get(f"{BASE}{path}", params=params, timeout=30)
    n[0] += 1
    if r.status_code == 429:
        raise SystemExit(
            "429 from ll.thespacedevs.com: the 15/hour limit is spent. "
            "Wait an hour. Nothing was written.")
    if r.status_code >= 400:
        raise SystemExit(f"HTTP {r.status_code}: {(r.text or '')[:300]}")
    body = r.json()
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        key.write_text(json.dumps(body), encoding="utf-8")
    except OSError:
        pass
    time.sleep(PAUSE_S)
    return body, False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--near-miss", action="store_true",
                    help="For each unmatched provider, show the closest "
                         "rows in `organizations`. Costs no API "
                         "requests - the manifest is already fetched.")
    args = ap.parse_args(argv)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    n = [0]
    from_cache = 0
    launches = []
    offset = 0
    total = None

    print("Launch Library 2 survey - read-only\n")
    while True:
        body, hit = fetch(session, "/launch/upcoming/",
                          {"limit": PAGE, "offset": offset, "mode": "list"},
                          n, use_cache=not args.no_cache)
        from_cache += hit
        if total is None:
            total = body.get("count")
            print(f"  upcoming launches            : {total}")
        results = body.get("results") or []
        launches.extend(results)
        print(f"    fetched {len(launches)}/{total}   requests {n[0]}   "
              f"cached {from_cache}")
        if not body.get("next") or not results:
            break
        offset += PAGE

    s = len(launches) or 1

    # ── NET is not a date ────────────────────────────────────────────
    prec = collections.Counter()
    status = collections.Counter()
    providers = collections.Counter()
    no_net = 0
    for l in launches:
        p = l.get("net_precision")
        prec[(p or {}).get("name") if isinstance(p, dict) else str(p)] += 1
        st = l.get("status")
        status[(st or {}).get("name") if isinstance(st, dict) else str(st)] += 1
        if l.get("lsp_name"):
            providers[str(l["lsp_name"]).strip()] += 1
        if not l.get("net"):
            no_net += 1

    print(f"\n  HOW WELL IS THE TIME ACTUALLY KNOWN?")
    print(f"    (net is 'no earlier than'. A launch known to the month "
          f"and one known to")
    print(f"     the second are both timestamps, and storing them alike "
          f"presents a guess")
    print(f"     as a fact - the error AD-061 caught on TRL.)")
    for name, cnt in prec.most_common():
        print(f"      {cnt:>5}  ({100*cnt/s:>4.0f}%)  {name}")
    print(f"      {no_net:>5}            no net at all")

    print(f"\n  STATUS")
    for name, cnt in status.most_common(10):
        print(f"      {cnt:>5}  {name}")

    # ── The decisive number ──────────────────────────────────────────
    engine = get_engine()
    with engine.connect() as conn:
        strict_idx, relaxed_idx = load_org_index(conn)

    by_how: collections.Counter = collections.Counter()
    matched, unmatched = {}, []
    for name, cnt in providers.items():
        code, how = match_org(name, strict_idx, relaxed_idx)
        by_how[how] += 1
        if code:
            matched[name] = (code, how, cnt)
        else:
            unmatched.append((name, cnt))

    d = max(1, len(providers))
    strict_n = by_how.get("strict", 0)
    loose_n = by_how.get("agency_prefix_stripped", 0)
    print(f"\n  PROVIDER JOIN  (predicted 25-55% of distinct names)")
    print(f"    distinct providers         : {len(providers)}")
    print(f"    STRICT                     : {strict_n} "
          f"({100*strict_n/d:.0f}%)")
    print(f"    +agency-prefix stripped    : +{loose_n} -> "
          f"{strict_n+loose_n} ({100*(strict_n+loose_n)/d:.0f}%)")
    print(f"    unmatched                  : {by_how.get(None, 0)}")

    if matched:
        print(f"\n    matched, most launches first:")
        for name, (code, how, cnt) in sorted(
                matched.items(), key=lambda kv: -kv[1][2])[:10]:
            flag = "" if how == "strict" else "  (loose)"
            print(f"      {code:<10}{cnt:>4}  {name[:44]}{flag}")
    if unmatched:
        print(f"\n    UNMATCHED, most launches first - read these:")
        for name, cnt in sorted(unmatched, key=lambda kv: -kv[1])[:12]:
            print(f"      {'':<10}{cnt:>4}  {name[:44]}")

    # ── WHY a name missed, not just THAT it did ──────────────────────
    #
    # The first run matched 55% and the misses were United Launch
    # Alliance (36 launches), CASC, Mitsubishi Heavy Industries and
    # Roscosmos - organisations GCAT plainly knows. That is a matcher
    # failure, not the population difference AD-083 describes, and the
    # two are worth telling apart before anyone edits a noise list.
    #
    # So: for each miss, the nearest names in `organizations`. It costs
    # no API requests - the manifest is already fetched - and it turns
    # "18 unmatched" into a list of specific, decidable cases.
    if args.near_miss and unmatched:
        import difflib
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT code, name_english, name_native, name_short
                  FROM organizations
            """)).all()
        candidates = []
        for code, eng, nat, short in rows:
            for label in (eng, nat, short):
                if label:
                    candidates.append((str(label), code))
        print(f"\n    NEAR MISSES - the closest rows in `organizations`")
        print(f"    (a close match means the matcher needs work; nothing "
              f"close means GCAT")
        print(f"     genuinely does not carry the provider, which is a "
              f"different problem)")
        for name, cnt in sorted(unmatched, key=lambda kv: -kv[1])[:10]:
            near = difflib.get_close_matches(
                name, [c[0] for c in candidates], n=3, cutoff=0.45)
            print(f"\n      {cnt:>4}  {name}")
            if not near:
                print(f"            nothing within reach - not in GCAT "
                      f"under any of its three names")
            for label in near:
                code = next(c[1] for c in candidates if c[0] == label)
                ratio = difflib.SequenceMatcher(None, name.lower(),
                                                label.lower()).ratio()
                print(f"            {code:<10}{ratio:>5.0%}  {label[:46]}")

    print(f"\n  COMPARE WITH TECHPORT: 3% strict, 17% combined. If this "
          f"lands high,")
    print(f"  AD-083 is a finding about TechPort's population. If it "
          f"lands near 3%,")
    print(f"  it is a finding about name-matching itself, and the next "
          f"importer needs")
    print(f"  a different plan.")

    print(f"\n  requests used: {n[0]} of {MAX_REQUESTS} budgeted "
          f"(15/hour free tier)   served from cache: {from_cache}")
    print("\n  Nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
