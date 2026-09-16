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


def load_org_index(conn) -> "tuple[dict, dict]":
    """
    Two indexes over `organizations`: strict, and agency-prefix-stripped.

    Kept apart so a caller can report what the looser rule buys. A merged
    index would report one rate and hide that most of it came from a rule
    nobody has agreed to.
    """
    strict: dict = {}
    relaxed: dict = {}
    rows = conn.execute(text("""
        SELECT code, name_english, name_native, name_short
          FROM organizations
    """))
    for code, eng, nat, short in rows:
        if code in PLACEHOLDER_CODES:
            continue
        for candidate in (eng, nat, short):
            k = norm(candidate)
            if k:
                strict.setdefault(k, code)
            r = norm_relaxed(candidate)
            if r:
                relaxed.setdefault(r, code)
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
