#!/usr/bin/env python
"""Re-match `research_organizations` against the current org index.

    python recheck_org_codes.py              # report. writes nothing
    python recheck_org_codes.py --apply      # rewrite the disagreements

WHY THIS IS OWED (AD-108)
=========================
Every `organization_code` in this database written before 2026-10-12 was
decided by an index that resolved collisions with `setdefault` over a
`SELECT` with no `ORDER BY`. 332 of 8,538 keys were claimed by more than
one code, and for those the winner was whatever order the heap returned
-- while `match_method='strict'` was recorded beside it.

The case that found it: "THE BOEING COMPANY" resolved to BLS, Boeing
Launch Services, parent BOHB, while BOE -- the parent -- sat in the same
index and lost. The value in such a row is not merely unverified; it was
never chosen.

WHAT THIS CAN AND CANNOT TELL YOU
=================================
It can tell you WHICH rows the two indexes disagree about, and it prints
both sides with their names and their parentage so a person can judge.

It cannot tell you the new answer is RIGHT. The new order is stated, not
correct: `'mda'` moved from the Missile Defense Agency to MacDonald
Dettwiler, two real MDAs that no tie-break can separate. So the report
splits the changes by direction, because that is the part that carries
meaning:

    to the parent      a subsidiary replaced by its own top-level org.
                       This is the Boeing shape, and it is the fix.
    to a subsidiary    a top-level org replaced by a child. Suspect.
                       Nothing should move this way; if it does, read it.
    sideways           neither row has a parent. A genuine ambiguity
                       between peers, and a candidate for a curated
                       refusal in org_aliases.tsv rather than a sort.

SCOPE
=====
`research_organizations` only, and that is not a shortcut -- it is the
only table that stores the SOURCE NAME beside the code it was matched
to, so it is the only one that can be re-matched from the database
alone. `cost_claims` was written after the fix. `satellites.operator_code`
is GCAT's own code, an exact join rather than a name match (011), so the
index never touched it.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from src.env import bootstrap

bootstrap()

from sqlalchemy import text                                  # noqa: E402

from src.catalog.org_match import (                          # noqa: E402
    load_aliases, load_org_index, match_org,
)
from src.db.writer import get_engine                         # noqa: E402

ORG_NAME = "coalesce(name_english, name_native, name_short)"


def confidence_for(how: "str | None") -> "float | None":
    """Exactly what seed_techport.py writes, so a rewritten row is
    indistinguishable from one the importer produced. Two implementations
    of one rule is how they drift."""
    return 1.0 if how == "strict" else 0.85 if how else None


def org_facts(conn) -> dict:
    return {r[0]: (r[1], r[2]) for r in conn.execute(text(
        f"SELECT code, {ORG_NAME}, parent_code FROM organizations"))}


def classify(old_code, old_how, new_code, new_how, facts) -> str:
    if old_code == new_code:
        return "agree" if old_how == new_how else "method only"
    if old_code is None:
        return "gained"
    if new_code is None:
        return "lost"
    old_parent = facts.get(old_code, (None, None))[1]
    new_parent = facts.get(new_code, (None, None))[1]
    if old_parent and not new_parent:
        return "to the parent"
    if new_parent and not old_parent:
        return "to a subsidiary"
    return "sideways"


def describe(code, facts) -> str:
    if code is None:
        return "-"
    name, parent = facts.get(code, ("(not in organizations)", None))
    sub = f"  [child of {parent}]" if parent else ""
    return f"{code} {str(name)[:40]}{sub}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Rewrite the disagreeing rows. Default is a "
                         "report that changes nothing.")
    ap.add_argument("--show", type=int, default=12,
                    help="Rows to print per class (default 12).")
    args = ap.parse_args(argv)

    engine = get_engine()
    with engine.connect() as conn:
        strict, relaxed = load_org_index(conn, report=True)
        aliases = load_aliases()
        facts = org_facts(conn)
        rows = conn.execute(text("""
            SELECT id, name, organization_code, match_method,
                   source_confidence
              FROM research_organizations
             ORDER BY id
        """)).all()

    verdicts: list = []
    for rid, name, old_code, old_how, _conf in rows:
        new_code, new_how = match_org(name, strict, relaxed, aliases)
        verdicts.append((classify(old_code, old_how, new_code, new_how,
                                  facts),
                         rid, name, old_code, old_how, new_code, new_how))

    counts = Counter(v[0] for v in verdicts)
    print(f"\n  research_organizations rows   {len(rows)}")
    print(f"  currently coded               "
          f"{sum(1 for r in rows if r[2])}")
    print("\n  AGREEMENT WITH THE CURRENT INDEX")
    for k in ("agree", "method only", "gained", "lost",
              "to the parent", "to a subsidiary", "sideways"):
        n = counts.get(k, 0)
        flag = ""
        if k == "to a subsidiary" and n:
            flag = "   <-- nothing should move this way"
        if k == "sideways" and n:
            flag = "   <-- peers; wants a curated decision, not a sort"
        print(f"    {k:<18}{n:>6}{flag}")

    changed = [v for v in verdicts if v[0] not in ("agree",)]
    for cls in ("to the parent", "to a subsidiary", "sideways",
                "gained", "lost", "method only"):
        sel = [v for v in changed if v[0] == cls]
        if not sel:
            continue
        print(f"\n  {cls.upper()}  ({len(sel)})")
        for _c, rid, name, oc, oh, nc, nh in sel[:args.show]:
            print(f"    #{rid} {str(name)[:52]}")
            print(f"        was  {oh or '-':<24}{describe(oc, facts)}")
            print(f"        now  {nh or '-':<24}{describe(nc, facts)}")
        if len(sel) > args.show:
            print(f"    ... and {len(sel) - args.show} more")

    if not changed:
        print("\n  Nothing disagrees. Either the collisions never touched "
              "these names,\n  or they were already resolving to the "
              "preferred code by luck.")
        return 0

    if not args.apply:
        print(f"\n  {len(changed)} row(s) would be rewritten. "
              "Re-run with --apply.")
        print("  Read the 'to a subsidiary' and 'sideways' lists first: "
              "those are the\n  ones where the new answer is defined "
              "rather than better.")
        return 0

    with engine.begin() as conn:
        for _c, rid, _name, _oc, _oh, nc, nh in changed:
            conn.execute(text("""
                UPDATE research_organizations
                   SET organization_code = :code,
                       match_method      = :how,
                       source_confidence = :conf,
                       matched_at        = CASE WHEN :code IS NULL
                                                THEN NULL ELSE NOW() END,
                       updated_at        = NOW()
                 WHERE id = :id
            """), {"id": rid, "code": nc, "how": nh,
                   "conf": confidence_for(nh)})

        # 015's CHECK is both-or-neither. Assert it held rather than
        # trusting that it did: the constraint would have raised, but a
        # control that only fires on failure tells you nothing on success.
        broken = conn.execute(text("""
            SELECT count(*) FROM research_organizations
             WHERE (organization_code IS NULL) <> (match_method IS NULL)
        """)).scalar_one()
        if broken:
            raise SystemExit(
                f"{broken} row(s) now have a code without a method or a "
                "method without a code. Rolling back.")

    print(f"\n  rewrote {len(changed)} row(s). "
          "research_org_match_complete still holds on every row.")
    print("  seed_techport.py will now agree with the database on a "
          "re-run, because\n  confidence_for() reproduces its own rule "
          "rather than inventing one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
