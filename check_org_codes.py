#!/usr/bin/env python
"""What does `organizations` actually hold, and what does match_org do with a name?

    python check_org_codes.py --code BLS LM RAYTH
    python check_org_codes.py --name "SPACE EXPLORATION TECHNOLOGIES CORP."
    python check_org_codes.py --recipients          # the 10-12 USAspending sample

WHY THIS EXISTS
===============
On 2026-10-12 the USAspending dry run matched 21 of 25 recipients and
reported 84%. The rate was true and told us nothing:

  - the 25 rows held about SIX distinct companies (Lockheed 14 times),
    so 84% measured contract concentration, not matching;
  - `SPACE EXPLORATION TECHNOLOGIES CORP.` did not match, which is
    SpaceX's legal name;
  - and `THE BOEING COMPANY` matched the code **BLS**, which does not
    read like The Boeing Company.

A match rate cannot distinguish a correct match from a confident wrong
one, and a wrong one here is AD-101's defect: a parent company's money
filed against a subsidiary's code groups silently and wrongly, and the
number looks perfectly reasonable while it does.

So: before any ingester writes `organization_code`, look at the row it
would write. Read-only, always.
"""
from __future__ import annotations

import argparse
import sys

from src.env import bootstrap

bootstrap()

from sqlalchemy import text                                  # noqa: E402

from src.catalog.org_match import (                          # noqa: E402
    PLACEHOLDER_CODES, load_aliases, load_org_index, match_org, norm,
    norm_relaxed,
)
from src.db.writer import get_engine                         # noqa: E402

#: The distinct recipients from the 2026-10-12 sample, both sort orders.
RECIPIENTS = [
    "THE BOEING COMPANY",
    "LOCKHEED MARTIN CORPORATION",
    "LOCKHEED MARTIN CORP",
    "RAYTHEON COMPANY",
    "NORTHROP GRUMMAN SYSTEMS CORP",
    "NORTHROP GRUMMAN SYSTEMS CORPORATION",
    "UNITED LAUNCH SERVICES, LLC",
    "SPACE EXPLORATION TECHNOLOGIES CORP.",
    "RAFAEL ADVANCED DEFENSE SYSTEMS LTD.",
    "ROCKWELL COLLINS, INC.",
]


def show_codes(conn, codes: "list[str]") -> None:
    rows = conn.execute(text(
        "SELECT * FROM organizations WHERE code = ANY(:c) ORDER BY code"
    ), {"c": codes}).mappings().all()
    found = {r["code"] for r in rows}
    print(f"\n  ORGANIZATIONS ROWS FOR {codes}")
    if not rows:
        print("    none of those codes exist")
    for r in rows:
        print(f"\n    code = {r['code']}")
        for k, v in r.items():
            if k == "code" or v in (None, ""):
                continue
            print(f"      {k:<22}{str(v)[:70]}")
    missing = [c for c in codes if c not in found]
    if missing:
        print(f"\n    absent: {missing}")


#: `organizations` has NO `name` column. It has name_english,
#: name_native, name_short and display_name, and the first version of
#: this file queried `name` and crashed -- the same mistake as importing
#: `tests.conftest`: a column guessed instead of read. load_org_index
#: indexes the first three.
NAME_COLS = "code, name_english, name_native, name_short, parent_code, org_type"


def show_names(conn, names: "list[str]") -> None:
    strict, relaxed = load_org_index(conn)
    aliases = load_aliases()
    print("\n  WHAT match_org DOES WITH EACH NAME")
    for nm in names:
        code, how = match_org(nm, strict, relaxed)
        print(f"\n  {nm}")
        print(f"      how={how or '-'}   code={code or '-'}")
        if not code:
            print(f"      norm         {norm(nm)!r}")
            print(f"      norm_relaxed {norm_relaxed(nm)!r}")
            print(f"      in aliases   {norm(nm) in aliases}")
            continue
        row = conn.execute(text(
            f"SELECT {NAME_COLS} FROM organizations WHERE code = :c"
        ), {"c": code}).mappings().first()
        if not row:
            print("      <-- code is not in organizations at all")
            continue
        for k in ("name_english", "name_native", "name_short", "org_type"):
            if row[k]:
                print(f"      {k:<14}{row[k]}")

        # THE DETECTOR THAT MATTERED. A parent company's name resolving
        # to a row that HAS a parent means the money is about to be filed
        # one level below where it was obligated -- AD-101's defect with
        # the value correct, which is the version nothing downstream
        # catches. Found 2026-10-12: "THE BOEING COMPANY" -> BLS, Boeing
        # Launch Services, parent_code BOHB.
        if row["parent_code"]:
            par = conn.execute(text(
                "SELECT name_native, name_short FROM organizations "
                "WHERE code = :c"), {"c": row["parent_code"]}).first()
            par_name = (par[0] or par[1]) if par else "(not in organizations)"
            print(f"      *** SUBSIDIARY: parent_code={row['parent_code']} "
                  f"({par_name})")
            print(f"          A claim coded here is attributed one level "
                  f"below the recipient.")

        # GCAT's own uncertainty marker. Matching a confident legal name
        # onto a row its own source is unsure about deserves saying.
        blob = " ".join(str(row[k] or "") for k in
                        ("name_english", "name_native", "name_short"))
        if "(?)" in blob:
            print("      *** the source itself marks this row uncertain: "
                  "'(?)'")

        # Which of the three indexed columns actually claimed the key?
        key = norm(nm)
        for k in ("name_english", "name_native", "name_short"):
            if row[k] and norm(row[k]) == key:
                note = ("   <-- a SHORT name is not a legal identity"
                        if k == "name_short" else "")
                print(f"      matched via {k}{note}")


def suggest(conn, names: "list[str]") -> None:
    """For each unmatched name, what IS in organizations that looks close?

    Candidates for `org_aliases.tsv`, printed and never written. A
    curated decision outranks every rule in the matcher, which is
    precisely why a person writes it and a diff records it.
    """
    strict, relaxed = load_org_index(conn)
    print("\n  CANDIDATES FOR org_aliases.tsv (nothing is written)")
    for nm in names:
        code, _how = match_org(nm, strict, relaxed)
        if code:
            continue
        tokens = [t for t in norm(nm).split()
                  if len(t) > 3 and t not in
                  ("corp", "corporation", "company", "inc", "llc", "ltd",
                   "systems", "technologies", "advanced", "services")]
        if not tokens:
            continue
        pats = [f"%{t}%" for t in tokens]
        like = conn.execute(text("""
            SELECT code, coalesce(name_english, name_native, name_short),
                   parent_code
              FROM organizations
             WHERE name_english ILIKE ANY(:p) OR name_native ILIKE ANY(:p)
                OR name_short ILIKE ANY(:p)
             ORDER BY 2 LIMIT 8
        """), {"p": pats}).all()
        print(f"\n    {nm}")
        print(f"      searched on {tokens}")
        if not like:
            print("      nothing in organizations resembles it — a genuine "
                  "absence, not a matcher failure")
        for c, n, parent in like:
            sub = f"   (subsidiary of {parent})" if parent else ""
            print(f"      {c:<10}{str(n)[:56]}{sub}")


def collisions(conn) -> None:
    """How many normalised keys are claimed by more than one org code?

    This is the blast radius of the 2026-10-12 finding, measured before
    anything is changed. `load_org_index` does:

        for candidate in (eng, nat, short):
            strict.setdefault(norm(candidate), code)

    `setdefault` means FIRST WRITER WINS, and the SELECT has no ORDER BY,
    so when two codes normalise to the same key the winner is whatever
    order the heap returns -- it can change after a vacuum or an update.
    The loser is discarded without a word, and `match_method='strict'` is
    recorded beside the winner, so the result does not merely hide a
    loosening: it misstates it.

    Four ingesters share this index. Measure first (AD-106): a matcher
    that several importers depend on is not something to change on the
    evidence of one sample.
    """
    rows = conn.execute(text(
        f"SELECT {NAME_COLS} FROM organizations")).mappings().all()
    claims: dict = {}
    for r in rows:
        if r["code"] in PLACEHOLDER_CODES:
            continue
        for kind in ("name_english", "name_native", "name_short"):
            k = norm(r[kind])
            if k:
                claims.setdefault(k, []).append((r["code"], kind,
                                                 r[kind], r["parent_code"]))
    multi = {k: v for k, v in claims.items()
             if len({c for c, _k, _n, _p in v}) > 1}

    # ASK THE RESOLVER WHO WINS. The first version of this function
    # reimplemented load_org_index's old setdefault order to decide the
    # winner -- so the moment the resolver gained a preference order, the
    # diagnostic would have kept reporting the old answer confidently.
    # A check that models the implementation drifts away from it; a check
    # that calls it cannot.
    strict, _relaxed = load_org_index(conn)

    short_only = {k: v for k, v in multi.items()
                  if all(kd == "name_short" for _c, kd, _n, _p in v)}
    parentchild = {k: v for k, v in multi.items()
                   if any(p for _c, _k, _n, p in v)}

    print("\n  STRICT-INDEX KEY COLLISIONS")
    print(f"    organizations rows            {len(rows)}")
    print(f"    distinct normalised keys      {len(claims)}")
    print(f"    keys claimed by >1 code       {len(multi)}")
    print(f"      ...where every claimant is a name_short   "
          f"{len(short_only)}")
    print(f"      ...involving a subsidiary (parent_code set) "
          f"{len(parentchild)}")
    print("\n    Each resolves to ONE code. Since 2026-10-12 that is the")
    print("    stated preference order -- fuller name form, then the top of")
    print("    the corporate tree, then code -- and not heap order.\n")
    for k, v in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:20]:
        winner = strict.get(k)
        print(f"    {k!r}  -> {winner}")
        for c, kd, n, p in v:
            mark = "  <-- wins" if c == winner else ""
            sub = f", parent={p}" if p else ""
            print(f"        {c:<8}{kd:<14}{str(n)[:40]}{sub}{mark}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--code", nargs="*", default=[])
    ap.add_argument("--name", nargs="*", default=[])
    ap.add_argument("--recipients", action="store_true",
                    help="Use the 2026-10-12 USAspending sample.")
    ap.add_argument("--collisions", action="store_true",
                    help="Report normalised keys claimed by >1 org code. "
                         "This is the blast radius of the name_short "
                         "finding.")
    args = ap.parse_args(argv)

    names = list(args.name)
    if args.recipients:
        names += RECIPIENTS
    codes = list(args.code)
    if not names and not codes and not args.collisions:
        ap.error("pass --code, --name, --recipients or --collisions")

    with get_engine().connect() as conn:
        if args.collisions:
            collisions(conn)
        if codes:
            show_codes(conn, codes)
        if names:
            show_names(conn, names)
            suggest(conn, names)
    print("\n  Read-only. Nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
