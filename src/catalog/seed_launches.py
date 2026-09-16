"""
Import the Launch Library 2 upcoming manifest. MVP piece 2.

    python -m src.catalog.seed_launches --apply

WHY THIS IS SHORT
=================
363 launches, one table, four requests. TechPort needed batching, a
watermark and a ten-hour plan; this fits in one run and always will,
because "upcoming" is bounded by definition. The whole difficulty here is
not volume - it is that the free tier is 15 requests/HOUR, so a careless
loop is not slow, it is a suspension. `check_launches.fetch` owns that
budget and this module does not open a second front door to the API.

NOTHING IS DELETED, EVER
========================
A launch leaves the manifest by happening. Deleting the rows that are
gone destroys the only record of what was expected and when - which is
the only way to ever ask whether a provider's dates slip - and 019 took
DELETE away from the pipeline role on purpose. Every row gets
`last_seen_at` stamped instead, and `upcoming_launches_current` is the
view that means "in the latest run".

WHAT IT REFUSES TO DECIDE
=========================
`net_confidence` is a GENERATED column in 020. This module does not
compute it, and must not: a tier decided in two places is a tier two
surfaces can disagree about.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from src.env import bootstrap

bootstrap()

import requests                                              # noqa: E402
from sqlalchemy import text                                  # noqa: E402

from src.catalog.org_match import (                          # noqa: E402
    load_org_index, match_org,
)
from src.db.writer import get_engine                         # noqa: E402
from src.useragent import user_agent                         # noqa: E402

# One client, one budget, one cache.
from check_launches import (                                  # noqa: E402
    BASE, MAX_REQUESTS, PAGE, fetch,
)

USER_AGENT = user_agent("upcoming launch manifest", "<=15 req/hr")


def _ts(v):
    """An ISO timestamp, or None. Never a guess."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse(raw: dict) -> "dict | None":
    """One manifest entry -> the row 020 wants. Pure: no IO."""
    lid = raw.get("id")
    if not lid or not raw.get("name"):
        return None
    st = raw.get("status") or {}
    prec = raw.get("net_precision") or {}
    return {
        "id": str(lid),
        "slug": raw.get("slug"),
        "name": str(raw["name"]).strip(),
        "status_id": st.get("id") if isinstance(st, dict) else None,
        "status_name": (st.get("name") if isinstance(st, dict)
                        else (str(st) if st else None)),
        "net": _ts(raw.get("net")),
        # Kept VERBATIM. The vocabulary is theirs, and a mapping is a
        # place to be wrong - 020's generated column handles the
        # unrecognised case by degrading, not by guessing.
        "net_precision": (prec.get("name") if isinstance(prec, dict)
                          else (str(prec) if prec else None)),
        "window_start": _ts(raw.get("window_start")),
        "window_end": _ts(raw.get("window_end")),
        "provider_name": (str(raw["lsp_name"]).strip()
                          if raw.get("lsp_name") else None),
        "mission": raw.get("mission"),
        "mission_type": raw.get("mission_type"),
        "pad": raw.get("pad"),
        "location": raw.get("location"),
        "image_url": raw.get("image"),
        "source_updated": _ts(raw.get("last_updated")),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Write. Without it, nothing is written.")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    n, from_cache, raw = [0], 0, []
    offset, total = 0, None
    print(f"Launch Library 2 -> upcoming_launches "
          f"({'APPLY' if args.apply else 'DRY RUN'})\n")
    while True:
        body, hit = fetch(session, "/launch/upcoming/",
                          {"limit": PAGE, "offset": offset, "mode": "list"},
                          n, use_cache=not args.no_cache)
        from_cache += hit
        if total is None:
            total = body.get("count")
            print(f"  upcoming launches            : {total}")
        results = body.get("results") or []
        raw.extend(results)
        if not body.get("next") or not results:
            break
        offset += PAGE
    print(f"  fetched                      : {len(raw)}   "
          f"requests {n[0]}/{MAX_REQUESTS}   cached {from_cache}")

    rows, unparseable = [], 0
    for r in raw:
        p = parse(r)
        if p is None:
            unparseable += 1
            continue
        rows.append(p)

    engine = get_engine()
    with engine.connect() as conn:
        strict_idx, relaxed_idx = load_org_index(conn)

    by_how = {}
    for r in rows:
        code, how = match_org(r["provider_name"], strict_idx, relaxed_idx)
        r["provider_code"], r["match_method"] = code, how
        by_how[how] = by_how.get(how, 0) + 1

    conf = {}
    for r in rows:
        if not r["net"] or not r["net_precision"]:
            k = "undated"
        elif r["net_precision"] in ("Second", "Minute", "Hour", "Day"):
            k = "dated"
        else:
            k = "approximate"
        conf[k] = conf.get(k, 0) + 1

    print(f"\n  HOW WELL THE TIME IS KNOWN (020 computes this itself; "
          f"shown here to compare)")
    for k in ("dated", "approximate", "undated"):
        c = conf.get(k, 0)
        print(f"    {k:<14}{c:>5}  ({100*c/max(1,len(rows)):>4.0f}%)")
    print(f"\n  PROVIDER LINK (an attribute, never a requirement)")
    for k in ("strict", "alias", "agency_prefix_stripped", None):
        if k in by_how:
            print(f"    {str(k or 'unmatched'):<24}{by_how[k]:>5}")

    if not args.apply:
        print(f"\n  Nothing was written. Re-run with --apply.")
        return 0

    stamp = datetime.now(timezone.utc)
    for r in rows:
        r["last_seen_at"] = stamp
    with engine.begin() as conn:
        before = conn.execute(text(
            "SELECT count(*) FROM upcoming_launches")).scalar()
        conn.execute(text("""
            INSERT INTO upcoming_launches
                (id, slug, name, status_id, status_name, net,
                 net_precision, window_start, window_end, provider_name,
                 provider_code, match_method, mission, mission_type, pad,
                 location, image_url, source_updated, last_seen_at,
                 fetched_at)
            VALUES (:id, :slug, :name, :status_id, :status_name, :net,
                    :net_precision, :window_start, :window_end,
                    :provider_name, :provider_code, :match_method,
                    :mission, :mission_type, :pad, :location, :image_url,
                    :source_updated, :last_seen_at, :last_seen_at)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                status_id = EXCLUDED.status_id,
                status_name = EXCLUDED.status_name,
                net = EXCLUDED.net,
                net_precision = EXCLUDED.net_precision,
                window_start = EXCLUDED.window_start,
                window_end = EXCLUDED.window_end,
                provider_name = EXCLUDED.provider_name,
                provider_code = EXCLUDED.provider_code,
                match_method = EXCLUDED.match_method,
                mission = EXCLUDED.mission,
                mission_type = EXCLUDED.mission_type,
                pad = EXCLUDED.pad,
                location = EXCLUDED.location,
                image_url = EXCLUDED.image_url,
                source_updated = EXCLUDED.source_updated,
                last_seen_at = EXCLUDED.last_seen_at,
                fetched_at = EXCLUDED.fetched_at,
                updated_at = NOW()
        """), rows)
        after = conn.execute(text(
            "SELECT count(*) FROM upcoming_launches")).scalar()
        # Rows the manifest no longer carries. NOT deleted - named.
        dropped = conn.execute(text("""
            SELECT count(*) FROM upcoming_launches
             WHERE last_seen_at < :s
        """), {"s": stamp}).scalar()

    print(f"\n  WRITTEN")
    print(f"    upcoming_launches          : {after:,} total "
          f"({after - before:+,} new)")
    print(f"    no longer in the manifest  : {dropped:,} "
          f"(kept, not deleted - they launched)")
    print(f"\n  SKIPPED")
    print(f"    entries that would not parse : {unparseable}")
    print(f"\n  requests used: {n[0]}   served from cache: {from_cache}")
    print(f"  Source: theSpaceDevs / Launch Library 2 - attribution "
          f"required wherever this renders.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
