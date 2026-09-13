"""
Load the curated TRL/MRL assessments into the platform.

    python -m src.catalog.seed_technologies --survey
    python -m src.catalog.seed_technologies --apply
    python -m src.catalog.seed_technologies --gap

Reads `data/seed/technologies.json` — 69 hand-written assessments across
13 categories, extracted 2026-09-12 from `technologies_readiness.py`,
where they had sat unused since June.

WHY THIS EXISTS BEFORE ANYTHING ELSE IN PHASE 3
===============================================
The main effort is industry, company and technology capability (restated
2026-09-12). Of everything that needs building, this is the only piece
whose data is already written: every other source has to be acquired,
matched and validated first. A table, an import and one view against
curated content is the cheapest real capability available.

The records span the WHOLE space domain — Launch Propulsion, Hypersonic
Systems, Reentry & Landing, On-Orbit Operations — not just satellites.
That was true in June, before the roadmap said it.

THE SEED FILE IS THE SOURCE OF TRUTH
====================================
Edit `data/seed/technologies.json`, then re-run with --apply. The
database is downstream of that file, never the other way round: a
readiness assessment is a considered judgement and belongs somewhere it
can be reviewed in a diff, not somewhere it can be UPDATEd at 2am.

`ref` ('lp-001') is the natural key, so re-running updates rather than
duplicating — and a TRL that changes shows up as an UPDATE with both
values visible in the survey.

ASSESSMENT BASIS IS REQUIRED
============================
AD-052: a TRL or MRL with no stated basis is an opinion. The column is
NOT NULL in 009 and this script refuses a record without one rather than
letting the database reject it later with a less useful message.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from src.env import load_env
    load_env()
except ImportError:
    pass

from sqlalchemy import text

from src.db.writer import get_engine, log_step

DEFAULT_SEED = "data/seed/technologies.json"

#: Hand-curated by a domain expert against named programmes. Not derived,
#: not scraped, not inferred - so 1.0, the same as SATCAT's exact join.
#: The confidence is in the *provenance*, not in the judgement being
#: beyond dispute; `assessment_basis` carries who is being relied on.
CURATED_CONFIDENCE = 1.0


def load_seed(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"seed file not found: {path}\n"
            f"Expected the curated TRL/MRL assessments. See "
            f"data/seed/README.md.")
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    for key in ("domain", "categories", "technologies"):
        if key not in data:
            raise SystemExit(f"{path} has no '{key}' key")

    known = {c["name"] for c in data["categories"]}
    problems = []
    for t in data["technologies"]:
        for field in ("ref", "name", "category", "trl", "mrl",
                      "assessment_basis"):
            if not t.get(field):
                problems.append(f"{t.get('ref', '?')}: missing {field}")
        if t.get("category") not in known:
            problems.append(f"{t.get('ref')}: category "
                            f"{t.get('category')!r} is not declared")
        for lvl in ("trl", "mrl"):
            v = t.get(lvl)
            if isinstance(v, int) and not 1 <= v <= 9:
                problems.append(f"{t['ref']}: {lvl}={v} outside 1-9")

    if problems:
        raise SystemExit(
            "seed file rejected — "
            f"{len(problems)} problem(s):\n  " + "\n  ".join(problems[:20])
            + "\n\nA missing assessment_basis is not a formatting nit: "
              "AD-052 says a readiness level with no stated basis is an "
              "opinion, and 009 will not store one.")
    return data


def survey(conn, data: dict) -> None:
    techs = data["technologies"]
    print(f"  seed file        : {len(techs)} technologies, "
          f"{len(data['categories'])} categories, domain "
          f"'{data['domain']}'")

    existing = {
        r[0]: (r[1], r[2]) for r in conn.execute(text("""
            SELECT t.ref, t.trl, t.mrl
              FROM technologies t
              JOIN domains d ON d.id = t.domain_id
             WHERE d.slug = :slug
        """), {"slug": data["domain"]})
    }
    new = [t for t in techs if t["ref"] not in existing]
    changed = [
        (t, existing[t["ref"]]) for t in techs
        if t["ref"] in existing
        and (t["trl"], t["mrl"]) != existing[t["ref"]]
    ]
    same = len(techs) - len(new) - len(changed)

    print(f"  already loaded   : {len(existing)}")
    print(f"  new              : {len(new)}")
    print(f"  changed TRL/MRL  : {len(changed)}")
    print(f"  unchanged        : {same}")
    for t, (old_trl, old_mrl) in changed[:10]:
        print(f"      {t['ref']}  {t['name'][:38]:<38} "
              f"TRL {old_trl}->{t['trl']}  MRL {old_mrl}->{t['mrl']}")

    # Show the shape before writing, not just the count (AD-049).
    gaps = Counter(t["trl"] - t["mrl"] for t in techs)
    print(f"\n  readiness gap distribution (TRL - MRL):")
    for g in sorted(gaps, reverse=True):
        bar = "#" * gaps[g]
        print(f"      {g:>+3}  {gaps[g]:>3}  {bar}")
    wide = [t for t in techs if t["trl"] - t["mrl"] >= 2]
    print(f"\n  proven but not manufacturable (gap >= 2): {len(wide)}")
    for t in sorted(wide, key=lambda t: t["trl"] - t["mrl"],
                    reverse=True)[:6]:
        print(f"      TRL {t['trl']} / MRL {t['mrl']}   "
              f"{t['name'][:44]:<44} {t['assessment_basis']}")


def apply(conn, data: dict) -> tuple:
    now = datetime.now(timezone.utc)

    domain_id = conn.execute(text(
        "SELECT id FROM domains WHERE slug = :slug"
    ), {"slug": data["domain"]}).scalar_one_or_none()
    if domain_id is None:
        raise SystemExit(
            f"domain '{data['domain']}' is not in `domains`. Apply "
            f"009_domains_and_technology.sql first.")

    cat_ids = {}
    for c in data["categories"]:
        cat_ids[c["name"]] = conn.execute(text("""
            INSERT INTO technology_categories (domain_id, slug, name)
            VALUES (:d, :s, :n)
            ON CONFLICT (domain_id, slug) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """), {"d": domain_id, "s": c["slug"], "n": c["name"]}).scalar_one()

    written = 0
    for t in data["technologies"]:
        conn.execute(text("""
            INSERT INTO technologies
                (domain_id, category_id, ref, name, trl, mrl,
                 assessment_basis, description, data_source, match_method,
                 source_confidence, matched_at, updated_at)
            VALUES
                (:d, :c, :ref, :name, :trl, :mrl, :basis, :desc,
                 'curated', 'ref', :conf, :now, :now)
            ON CONFLICT (domain_id, ref) DO UPDATE SET
                category_id       = EXCLUDED.category_id,
                name              = EXCLUDED.name,
                trl               = EXCLUDED.trl,
                mrl               = EXCLUDED.mrl,
                assessment_basis  = EXCLUDED.assessment_basis,
                description       = EXCLUDED.description,
                data_source       = EXCLUDED.data_source,
                match_method      = EXCLUDED.match_method,
                source_confidence = EXCLUDED.source_confidence,
                matched_at        = EXCLUDED.matched_at,
                updated_at        = EXCLUDED.updated_at
        """), {
            "d": domain_id, "c": cat_ids[t["category"]], "ref": t["ref"],
            "name": t["name"], "trl": t["trl"], "mrl": t["mrl"],
            "basis": t["assessment_basis"],
            "desc": t.get("description"), "conf": CURATED_CONFIDENCE,
            "now": now,
        })
        written += 1
    return written, len(cat_ids)


def show_gap(conn, domain: str) -> None:
    """The view this epic exists for."""
    rows = list(conn.execute(text("""
        SELECT c.name, t.name, t.trl, t.mrl, t.readiness_gap,
               t.assessment_basis
          FROM technologies t
          JOIN domains d ON d.id = t.domain_id
          LEFT JOIN technology_categories c ON c.id = t.category_id
         WHERE d.slug = :slug AND t.readiness_gap >= 2
         ORDER BY t.readiness_gap DESC, c.name, t.name
    """), {"slug": domain}))
    print(f"\n  Proven faster than it can be built — TRL exceeds MRL by 2+")
    print(f"  {'gap':>4}  {'TRL':>3} {'MRL':>3}  {'category':<24}"
          f"{'technology':<44} basis")
    print("  " + "-" * 104)
    for cat, name, trl, mrl, gap, basis in rows:
        print(f"  {gap:>4}  {trl:>3} {mrl:>3}  {(cat or '?')[:23]:<24}"
              f"{name[:43]:<44} {basis}")
    print(f"\n  {len(rows)} technologies. This is the set where capability "
          f"has been\n  demonstrated and the supply chain has not caught up.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", default=os.environ.get(
        "TECHNOLOGY_SEED", DEFAULT_SEED))
    ap.add_argument("--survey", action="store_true",
                    help="Report what would change, then exit.")
    ap.add_argument("--apply", action="store_true",
                    help="Write. Without this nothing is written.")
    ap.add_argument("--gap", action="store_true",
                    help="Show the TRL-MRL gap view and exit.")
    args = ap.parse_args(argv)

    engine = get_engine()

    if args.gap:
        with engine.connect() as conn:
            show_gap(conn, load_seed(Path(args.seed))["domain"])
        return 0

    data = load_seed(Path(args.seed))
    print(f"technologies.json — validated, "
          f"{len(data['technologies'])} records")

    with engine.connect() as conn:
        print()
        survey(conn, data)

    if not args.apply:
        print("\nNothing written. Re-run with --apply.")
        return 0

    run_id = str(uuid.uuid4())
    try:
        with engine.begin() as conn:
            written, cats = apply(conn, data)
    except Exception as exc:
        try:
            log_step(run_id, pipeline="technology_seed", step="write_db",
                     status="failed", message=str(exc)[:500])
        except Exception as log_exc:                        # noqa: BLE001
            print(f"(could not write the failure to ingestion_log: "
                  f"{log_exc})", file=sys.stderr)
        raise
    log_step(run_id, pipeline="technology_seed", step="write_db",
             status="success", records_processed=written,
             message=f"{written} technologies, {cats} categories, domain "
                     f"{data['domain']}", source="curated")
    print(f"\nWrote {written} technologies across {cats} categories.")

    with engine.connect() as conn:
        show_gap(conn, data["domain"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
