"""
One matcher, and the labels it produces are a schema contract.

WHY THE LABEL TEST MATTERS MOST
===============================
`015_research_activity.sql` requires `organization_code` and
`match_method` to be written together, so that a match made under a
loosened rule stays distinguishable from a strict one (AD-085). That only
works if every caller spells the loosened rule the same way.

They did not. When the matcher was extracted on 2026-09-15,
`check_techport.py` was found to be labelling it `"relaxed"` while the
migration and the importer call it `"agency_prefix_stripped"` - two
values of one rule, inside one file, on the way to a column whose whole
purpose is to tell the two rules apart. Nothing would have errored; the
database would simply have held a `match_method` that meant the same
thing twice.

Offline. No database, no network.
"""
from __future__ import annotations

import pytest

from src.catalog.org_match import (
    NORM_VERSION, PLACEHOLDER_CODES, load_aliases, match_org, norm,
    norm_relaxed,
)

#: The vocabulary 015 and the importer agree on.
STRICT = "strict"
LOOSE = "agency_prefix_stripped"


# ── Normalisation ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Starfish Space, Inc.", "starfish space"),
    ("Advanced Space, LLC", "advanced space"),
    # "technology" joined the noise list on 2026-09-15, so Caltech now
    # normalises to "california institute". Checked before changing it:
    # that key still resolves uniquely to GCAT's CIT and nothing else,
    # so the R&D layer's existing link is unaffected. The expectation
    # moved because the rule moved, not to make a red test green.
    ("California Institute of Technology", "california institute"),
    ("A10 Systems Inc.", "a10 systems"),
    # "&" becomes " and ", and "and" is itself in the noise list, so it
    # is stripped straight back out. Asserted as it actually behaves
    # rather than as it reads.
    ("Research & Development Corp", "research development"),
    ("  Multiple   Spaces  ", "multiple spaces"),
])
def test_norm(raw: str, expected: str) -> None:
    assert norm(raw) == expected


@pytest.mark.parametrize("empty", [None, "", "   ", ",,,", "Inc."])
def test_norm_of_nothing_is_empty(empty) -> None:
    """
    An empty key must never be stored. `name_norm` is UNIQUE in 015, so
    two differently-named organisations both normalising to '' would
    collide on a key that means nothing.
    """
    assert norm(empty) == ""


def test_relaxed_strips_only_a_leading_agency() -> None:
    assert norm_relaxed("NASA Goddard Space Flight Center") == \
        "goddard space flight center"
    # Not a leading agency: must survive.
    assert "nasa" in norm_relaxed("Jet Propulsion Laboratory NASA")


def test_relaxed_is_a_superset_of_strict() -> None:
    """Anything strict normalises to, relaxed must reach too."""
    for name in ("Boeing", "NASA Ames Research Center", "Tendeg, LLC"):
        assert norm_relaxed(name) in (norm(name),
                                      norm(name).split(" ", 1)[-1])


# ── The labels, which are a schema contract ──────────────────────────

def test_strict_match_is_labelled_strict() -> None:
    code, how = match_org("Boeing", {"boeing": "BOEING"}, {})
    assert (code, how) == ("BOEING", STRICT)


def test_loose_match_uses_the_name_015_expects() -> None:
    """
    NOT cosmetic. 015 stores this string in `match_method`, and the whole
    point of the column is that a loosened match stays visible as one.
    Two spellings of one rule make it invisible again.
    """
    code, how = match_org(
        "Goddard Space Flight Center", {},
        {"goddard space flight center": "GSFC"})
    assert code == "GSFC"
    assert how == LOOSE, (
        f"the loose match is labelled {how!r}; 015 and the importer "
        f"expect {LOOSE!r}"
    )


def test_strict_wins_when_both_indexes_would_match() -> None:
    """
    A name reachable strictly must never be recorded as a loose match -
    that would overstate how much the loosening is buying.
    """
    code, how = match_org("Ames Research Center",
                          {"ames research center": "ARC"},
                          {"ames research center": "SOMETHING_ELSE"})
    assert (code, how) == ("ARC", STRICT)


def test_no_match_returns_two_nones() -> None:
    """
    Both, not one. 015's CHECK refuses a code without a method and a
    method without a code, so a half-populated return would become a
    constraint violation at write time rather than a NULL link.
    """
    assert match_org("Thinkorbital Inc.", {}, {}) == (None, None)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_name_never_matches(blank) -> None:
    assert match_org(blank, {"": "WRONG"}, {"": "WRONG"}) == (None, None)


def test_norm_version_is_set() -> None:
    """
    name_norm is a UNIQUE key computed by these rules. If they change,
    stored keys stop matching newly-computed ones and an upsert inserts a
    duplicate instead of updating. The version is what makes that
    detectable rather than a slow leak of near-identical rows.
    """
    assert isinstance(NORM_VERSION, int) and NORM_VERSION >= 1


# ── Added 2026-09-15, from the Launch Library 2 survey ───────────────

ALIAS = "alias"


@pytest.mark.parametrize("raw,expected", [
    # GCAT carries these under a name ending in "Technology" that the
    # provider itself does not use. Three launch providers missed on it.
    ("Orienspace Technology", "orienspace"),
    ("LandSpace Technology", "landspace"),
    ("ExPace Technology Corp.", "expace"),
    ("Advanced Space Technologies", "advanced space"),
])
def test_technology_is_noise(raw: str, expected: str) -> None:
    assert norm(raw) == expected


def test_a_curated_alias_is_labelled_as_one(monkeypatch) -> None:
    """
    An alias is a human decision and must stay distinguishable from a
    rule, for the same reason AD-085 keeps the loosened match visible:
    a judgement that reads as a derivation cannot be reviewed.
    """
    code, how = match_org("Mitsubishi Heavy Industries", {}, {})
    assert (code, how) == ("MHI", ALIAS)


def test_a_refusal_beats_a_rule_that_would_have_matched() -> None:
    """
    THE ONE THAT MATTERS. The nearest GCAT row to CASC is CASIC - China
    Aerospace Science and INDUSTRY Corporation, a different company. If
    a future index or noise change makes some rule match it, the curated
    refusal must still win, or ten launches go to the wrong organisation
    quietly.
    """
    tempting = {norm("China Aerospace Science and Technology Corporation"):
                "CASIC"}
    code, how = match_org(
        "China Aerospace Science and Technology Corporation",
        tempting, tempting)
    assert (code, how) == (None, None), (
        "a curated refusal was overridden by a matching rule"
    )


def test_ula_stays_unmatched() -> None:
    """
    GCAT models ULA as ULAB, ULAL and ULAD - two JV parents and a
    factory - with no row meaning the launch provider. 36 launches, 10%
    of the manifest, and NULL is the honest answer.
    """
    assert match_org("United Launch Alliance", {}, {}) == (None, None)


def test_a_placeholder_never_matches_a_real_org() -> None:
    """
    Launch Library writes "Unknown" for a missing provider. GCAT's X is
    Type CY - an unknown COUNTRY - with ShortName "UNKNOWN", so a strict
    match linked two launches to a country placeholder. Refused on both
    sides; this asserts the alias side.
    """
    assert match_org("Unknown", {"unknown": "X"}, {}) == (None, None)


def test_placeholder_codes_are_named() -> None:
    assert {"X", "UNK"} <= set(PLACEHOLDER_CODES)


def test_every_alias_row_states_a_basis() -> None:
    """
    A match or a refusal without a reason is an assertion nobody can
    review, and the refusals are the ones that will be re-litigated.
    """
    from src.catalog.org_match import ALIAS_FILE
    rows = [l for l in ALIAS_FILE.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")
            and not l.lower().startswith("alias\t")]
    assert rows, "the alias file is empty"
    for line in rows:
        parts = line.split("\t")
        assert len(parts) >= 4, f"no basis column: {line[:60]}"
        assert parts[2] in ("match", "refuse"), f"bad decision: {parts[2]}"
        assert len(parts[3].strip()) > 40, (
            f"basis too thin to review: {parts[0]} -> {parts[3][:40]!r}")


def test_norm_version_was_bumped_with_the_rules() -> None:
    """
    name_norm is a UNIQUE key computed by these rules. Changing them
    without bumping the version is how an upsert silently inserts a
    duplicate of a row that is already there.
    """
    assert NORM_VERSION >= 2
