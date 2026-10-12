"""
One implementation of "is this the same organisation".

WHY THIS IS A MODULE AND NOT A COPY
===================================
On 2026-09-14 this project lost a day to a 403 that a descriptive
User-Agent fixed in one header - and the uncomfortable half of that
finding was that four call sites already sent one, in three different
strings, with no shared definition, so the fifth client had nothing to
reuse. A convention kept in N copies is a convention a new caller cannot
find.

`check_techport.py` grew `norm`, `norm_relaxed` and `load_org_index` as a
survey's local helpers. `015_research_activity.sql` then made
`research_organizations.name_norm` a UNIQUE upsert key computed by the
importer, which means the matcher and the importer MUST agree on what
normalisation is - exactly, forever, including the next time someone adds
a suffix to the noise list. Two implementations of that rule is not a
tidiness problem; it is a silent duplicate-rows problem the day they
diverge.

So it moves here once, before the second caller exists, rather than after.

WHAT THE STRICT/RELAXED SPLIT IS FOR
====================================
GCAT stores "NASA Goddard Space Flight Center"; TechPort says "Goddard
Space Flight Center". Stripping the agency prefix is a REAL loosening of
the match and it is kept as its own index so its cost can be reported
separately (AD-085 needs a relaxed match to stay distinguishable from a
strict one after it is written, and a single merged index would report
one rate while hiding where it came from).

Measured 2026-09-14, systematic sample of 19,690 TechPort projects:
strict matched 1 distinct lead organisation in 35; relaxed added five,
and all five were NASA field centres.
"""
from __future__ import annotations

import pathlib
import re
from functools import lru_cache

from sqlalchemy import text

#: Tokens that differ between catalogues without changing the organisation.
#: `technology`/`technologies` were added 2026-09-15, after a Launch
#: Library survey missed Orienspace, LandSpace and ExPace - each of which
#: GCAT carries under a name ending in "Technology" that the provider
#: does not use.
#:
#: MEASURED BEFORE ADDING, because a noise word merges organisations and
#: a merge is silent. Across GCAT's 4,109 rows the number of normalised
#: keys mapping to more than one org code went 330 -> 332. The three new
#: collisions are same-entity variants (CASC6A and CASC6A1 are a parent
#: and its child, both literally "Academy of Aerospace Propulsion
#: Technology"). No distinct organisations were merged.
_NOISE = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|"
    r"gmbh|plc|sa|ab|bv|the|of|and|technology|technologies)\b\.?", re.I)

#: Agency prefixes that one catalogue carries and the other does not.
_AGENCY_PREFIX = re.compile(
    r"^(nasa|esa|jaxa|isro|usaf|us air force|us space force|dod|noaa)\s+")

#: Bumped when the rules below change in a way that alters output.
#:
#: research_organizations.name_norm is a UNIQUE key computed with these
#: rules. Change them and previously-stored keys no longer match what the
#: importer would now compute, so an upsert silently inserts a duplicate
#: of an organisation that is already there. A version the importer can
#: record and compare is the difference between that being detectable and
#: being a slow leak of near-identical rows.
#: 2 as of 2026-09-15: `technology`/`technologies` joined the noise list.
NORM_VERSION = 2


def norm(s: "str | None") -> str:
    """Lowercase, strip punctuation and corporate noise, collapse spaces."""
    if not s:
        return ""
    s = s.lower().replace("&", " and ")
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_relaxed(s: "str | None") -> str:
    """`norm`, and also without a leading agency name."""
    return _AGENCY_PREFIX.sub("", norm(s)).strip()


#: GCAT's own placeholders, which are not organisations.
#:
#: `X` is Type CY - an unknown COUNTRY - with ShortName "UNKNOWN". `UNK`
#: is a similar catch-all. Left in the index they match any source that
#: writes "Unknown" for a missing value, which Launch Library 2 does:
#: two upcoming launches would have been attributed to a country
#: placeholder, plausibly and wrongly. Found 2026-09-15 by reading the
#: row instead of trusting the match.
PLACEHOLDER_CODES = frozenset({"X", "UNK"})


#: Which name form may claim a key, best first. A SHORT name is a
#: nickname, not a legal identity: 164 of the 332 colliding keys found on
#: 2026-10-12 were claimed only by name_short values.
_FORM_RANK = {"name_english": 0, "name_native": 1, "name_short": 2}


def load_org_index(conn, report: bool = False) -> "tuple[dict, dict]":
    """
    Two indexes over `organizations`: strict, and agency-prefix-stripped.

    Kept apart so a caller can report what the looser rule buys. A merged
    index would report one rate and hide that most of it came from a rule
    nobody has agreed to.

    THE COLLISION FIX, 2026-10-12
    =============================
    This function used to do:

        for candidate in (eng, nat, short):
            strict.setdefault(norm(candidate), code)

    with no ORDER BY on the SELECT. `setdefault` means FIRST WRITER WINS,
    and without an ORDER BY the first writer is whatever order the heap
    returns -- so the answer could change after a vacuum or an update,
    and the loser was discarded without a word while `match_method`
    recorded 'strict' beside the winner. The result did not merely hide a
    loosening; it misstated it.

    Measured before changing anything: of 8,538 distinct keys over 4,108
    rows, **332 were claimed by more than one code**, 164 of those by
    name_short values alone, and 217 involved a subsidiary.

    The case that found it: `norm("THE BOEING COMPANY")` is 'boeing', and
    five codes claim that key --

        BLS   name_short  Boeing   parent=BOHB   <-- heap order won
        BO2   name_short  Boeing   parent=BOE
        BOC   name_short  Boeing   parent=BOE
        BOE   name_native Boeing Co.
        BOE   name_short  Boeing

    BLS is Boeing Launch Services, whose own name carries GCAT's
    uncertainty marker. **BOE, the parent, was in the index and lost.**
    $22.65bn of ISS obligations was one `--apply` away from being
    attributed one level below the company that was awarded it, with the
    value perfectly correct -- AD-101's defect in the form nothing
    downstream catches.

    So a key is now resolved by a stated preference rather than by luck:

        1. fuller name form first  (english < native < short)
        2. then the top of the corporate tree  (parent_code IS NULL)
        3. then code, so the result is STABLE and reproducible

    Rule 2 is what saves Boeing: all four claimants there tie at
    name_short, and only BOE has no parent.

    DELIBERATELY NOT FIXED HERE
    ===========================
    `name_short` still claims STRICT keys. Removing it would change match
    rates for every importer that uses this index -- including the
    TechPort and research-organisation figures recorded on 09-14 under
    AD-062 and AD-083 -- and those numbers would have to be re-measured
    before anyone could say whether the change helped. Measuring first is
    the rule (AD-106); this commit makes the index defined and stable,
    and leaves that question open rather than answering it blind.

    A consequence worth stating: `organization_code` values written
    BEFORE today came from the heap-order index. Some of them are
    subsidiaries chosen by luck. Nothing here rewrites them.

    `report=True` prints how many keys needed the preference order. A
    collision that is resolved silently is how this survived a month.
    """
    s_cand: dict = {}
    r_cand: dict = {}
    rows = conn.execute(text("""
        SELECT code, name_english, name_native, name_short, parent_code
          FROM organizations
         ORDER BY code
    """)).all()
    for code, eng, nat, short, parent in rows:
        if code in PLACEHOLDER_CODES:
            continue
        for kind, cand in (("name_english", eng),
                           ("name_native", nat),
                           ("name_short", short)):
            if not cand:
                continue
            score = (_FORM_RANK[kind], 1 if parent else 0, code)
            k = norm(cand)
            if k:
                s_cand.setdefault(k, []).append((score, code))
            r = norm_relaxed(cand)
            if r:
                r_cand.setdefault(r, []).append((score, code))

    def _resolve(cand: dict) -> "tuple[dict, int]":
        out, collided = {}, 0
        for k, lst in cand.items():
            lst.sort()
            out[k] = lst[0][1]
            if len({c for _s, c in lst}) > 1:
                collided += 1
        return out, collided

    strict, n_strict = _resolve(s_cand)
    relaxed, n_relaxed = _resolve(r_cand)

    if report:
        print(f"  org index: {len(rows)} rows, {len(strict)} strict keys, "
              f"{n_strict} decided by the preference order "
              f"({n_relaxed} relaxed)")
    return strict, relaxed


#: Curated name -> org code decisions, and the deliberate refusals.
#: A seed file in the repository rather than a table, for AD-052's
#: reason: a curated judgement belongs where it shows up in a diff, not
#: where it can be UPDATEd at 2am.
ALIAS_FILE = (pathlib.Path(__file__).resolve().parent.parent.parent
              / "data" / "seed" / "org_aliases.tsv")


@lru_cache(maxsize=1)
def load_aliases() -> "dict[str, str | None]":
    """
    -> {normalised alias: org code}, with None meaning REFUSED.

    A refusal is as much a decision as a match and is stored the same
    way, because the alternative is that someone helpfully "fixes" it
    later. The file records why for each one.

    The refusals are not hypothetical. The nearest GCAT row to "China
    Aerospace Science and Technology Corporation" is CASIC - China
    Aerospace Science and INDUSTRY Corporation, a different company. A
    fuzzy matcher, or an alias added without reading the row, would have
    attributed ten launches to the wrong organisation silently.
    """
    out: "dict[str, str | None]" = {}
    if not ALIAS_FILE.is_file():
        return out
    for line in ALIAS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        alias, code, decision = (p.strip() for p in parts[:3])
        if alias.lower() == "alias":            # header
            continue
        key = norm(alias)
        if not key:
            continue
        out[key] = code if decision == "match" and code not in ("", "-") else None
    return out


def match_org(name: "str | None", strict: dict, relaxed: dict,
              aliases: "dict | None" = None) -> tuple:
    """
    -> (code, how) where `how` is 'strict' | 'agency_prefix_stripped' | None.

    `how` is returned rather than inferred by the caller because
    015 requires organization_code and match_method to be written
    together - a code with no method makes a loosened match
    indistinguishable from a strict one, which is how a loosening becomes
    invisible.
    """
    # GUARD ON THE NORMALISED VALUE, NOT THE RAW ONE.
    #
    # `if not name` passes "   " straight through, and norm("   ") is "".
    # An empty key then looks up whatever happens to sit under "" in the
    # index - so a blank organisation name would be confidently matched
    # to an arbitrary org code, with match_method='strict' beside it.
    #
    # In practice load_org_index() never stores an empty key, so nothing
    # has been mismatched. But "it cannot happen because the only caller
    # today builds the index carefully" is the kind of safety that
    # disappears the moment a second caller exists - and the second
    # caller is the importer, which is why this was found on the day the
    # matcher was extracted. Found by a test, 2026-09-15.
    key = norm(name)
    if not key:
        return None, None

    # A curated decision outranks every rule below it, INCLUDING a
    # refusal. `aliases` defaults to the seed file; pass {} to match
    # without it.
    al = load_aliases() if aliases is None else aliases
    if key in al:
        code = al[key]
        return (code, "alias") if code else (None, None)

    code = strict.get(key)
    if code:
        return code, "strict"
    relaxed_key = norm_relaxed(name)
    if not relaxed_key:
        return None, None
    code = relaxed.get(relaxed_key)
    if code:
        return code, "agency_prefix_stripped"
    return None, None
