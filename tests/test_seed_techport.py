"""
The importer refuses to guess, and the thing it refuses to guess about is
tested without a network call.

WHY THE REFUSAL IS THE TEST
===========================
`015_research_activity.sql` records an open question rather than
answering it from memory: TechPort's `leadOrganization` is an object, the
09-14 survey read only `organizationName` from it, and nobody has
established whether it carries a stable id. That decides whether
`research_organizations` is upserted on `techport_org_id` or on
`name_norm` - a primary-key decision, which is the expensive kind to get
wrong because it is not wrong until there are rows, and by then it is a
migration.

So `--apply` exits non-zero and says why. This asserts it keeps doing
that, because "we'll remember to check first" is not a control.

`read_lead` is tested on synthetic payloads covering the shapes TechPort
might return, including the two that would be silently mishandled: a
plain string instead of an object, and an object with no id at all.

Offline: no requests, no database.
"""
from __future__ import annotations

import pytest

from src.catalog.seed_techport import ID_KEYS, main, read_lead


def test_apply_is_not_the_default(capsys) -> None:
    """
    `--apply` refused to exist at all until the 2026-09-15 survey
    answered whether leadOrganization carries an id (it does:
    `organizationId`, 50 of 50). Now that it is implemented, the
    remaining guarantee is narrower and still worth pinning: a run with
    no flags must not write.
    """
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    assert ap.parse_args([]).apply is False


def test_survey_is_the_default_action(capsys) -> None:
    """
    A run with no flags must not be able to write. The GCAT and UCS
    importers set this precedent: survey before apply, always, and the
    safe thing is what you get for free.
    """
    import src.catalog.seed_techport as m
    called = {}

    def fake_key():
        called["key"] = True
        raise SystemExit("stopped before any request")

    original = m._key
    m._key = fake_key
    try:
        with pytest.raises(SystemExit):
            main([])
    finally:
        m._key = original
    assert called.get("key"), "the default path should reach the survey"


# ── read_lead: the shapes that decide the schema ─────────────────────

@pytest.mark.parametrize("key", ID_KEYS)
def test_an_id_is_found_under_any_accepted_key(key: str) -> None:
    name, id_key, id_value, shape = read_lead(
        {"leadOrganization": {"organizationName": "Goddard", key: 4242}})
    assert (name, id_key, id_value, shape) == ("Goddard", key, 4242, "dict")


def test_an_object_with_no_id_is_reported_as_such() -> None:
    """
    The case that keeps `name_norm` as the upsert key. It must come back
    as a dict with no id - NOT as an absence, which would read as
    TechPort having no lead organisation at all.
    """
    name, id_key, _v, shape = read_lead(
        {"leadOrganization": {"organizationName": "Tendeg, LLC",
                              "organizationType": "INDUSTRY"}})
    assert (name, id_key, shape) == ("Tendeg, LLC", None, "dict")


def test_a_bare_string_is_reported_by_its_type() -> None:
    """
    Column drift, in miniature. If TechPort ever returns a plain string,
    a survey that silently coped would hide the change - the precedent
    being `fetch_gcat_catalog()`, which carries a drift warning because
    GCAT has reordered columns before.
    """
    name, id_key, _v, shape = read_lead({"leadOrganization": "Creare, LLC"})
    assert (name, id_key, shape) == ("Creare, LLC", None, "str")


@pytest.mark.parametrize("payload", [
    {}, {"leadOrganization": None}, {"leadOrganization": {}},
])
def test_absence_is_distinguishable_from_an_empty_object(payload) -> None:
    name, id_key, _v, shape = read_lead(payload)
    assert name is None and id_key is None
    assert shape in ("absent", "dict")


def test_a_zero_id_is_not_treated_as_missing() -> None:
    """
    `if lead.get(key)` would drop id 0. Ids start at 1 in practice, so
    this has never fired - which is exactly why it is worth pinning
    before someone simplifies the check.
    """
    _n, id_key, id_value, _s = read_lead(
        {"leadOrganization": {"organizationName": "x", "organizationId": 0}})
    assert id_key == "organizationId" and id_value == 0


# ── parse_project: one detail in, the rows 015 wants out ─────────────

from datetime import date                                    # noqa: E402

from src.catalog.seed_techport import _date, parse_project    # noqa: E402


def _detail(**over):
    p = {"projectId": 12345, "title": "Quantum Computer",
         "status": "Active", "trlCurrent": 7,
         "startDate": "2024-01-15", "endDate": "2026-12-31",
         "lastUpdated": "2026-9-10",
         "leadOrganization": {"organizationName": "Goddard Space Flight Center",
                              "organizationId": 42,
                              "organizationType": "NASA_CENTER"},
         "primaryTaxonomyNodes": [{"code": "TX11.6.4",
                                   "title": "Quantum Computer"}]}
    p.update(over)
    return {"project": p}


def test_a_whole_project_parses() -> None:
    r = parse_project(_detail())
    assert r["techport_id"] == 12345
    assert r["trl_current"] == 7
    assert r["start_date"] == date(2024, 1, 15)
    assert r["org"]["techport_org_id"] == 42
    assert r["org"]["name_norm"] == "goddard space flight center"
    assert r["nodes"] == [{"tx_code": "TX11.6.4",
                           "tx_title": "Quantum Computer"}]


@pytest.mark.parametrize("raw,expected", [
    ("2026-09-10", date(2026, 9, 10)),
    # No zero padding. This is what TechPort's listing actually returns,
    # and date.fromisoformat rejects it on Python < 3.11.
    ("2026-9-10", date(2026, 9, 10)),
    ("2026-09", date(2026, 9, 1)),
    ("2026", date(2026, 1, 1)),
    ("2026-09-10T00:00:00Z", date(2026, 9, 10)),
])
def test_date_formats_that_appear_in_the_wild(raw, expected) -> None:
    assert _date(raw) == expected


@pytest.mark.parametrize("junk", [None, "", "  ", "not a date", "0000-00-00"])
def test_unparseable_dates_become_none(junk) -> None:
    assert _date(junk) is None


def test_unparseable_dates_are_counted_not_swallowed() -> None:
    """
    A silent NULL is how a column quietly becomes empty while the import
    reports success. The count is what makes it visible.
    """
    r = parse_project(_detail(startDate="soon", endDate="later"))
    assert r["start_date"] is None and r["end_date"] is None
    assert r["date_misses"] == 2


def test_a_taxonomy_node_that_would_violate_the_check_is_skipped_and_kept() -> None:
    """
    015's CHECK is the authority; this keeps ONE malformed node from
    aborting a ten-hour import — and lists it rather than dropping it
    quietly.
    """
    r = parse_project(_detail(primaryTaxonomyNodes=[
        {"code": "TX11.6.4", "title": "Good"},
        {"code": "Sensors and Instruments", "title": "A title in the code"},
        {"code": "", "title": "No code at all"},
    ]))
    assert [n["tx_code"] for n in r["nodes"]] == ["TX11.6.4"]
    assert len(r["bad_nodes"]) == 2


@pytest.mark.parametrize("trl,expected", [
    (7, 7), (1, 1), (9, 9),
    (0, None), (10, None), (None, None), ("7", None),
])
def test_trl_outside_the_schema_range_never_reaches_the_database(trl, expected) -> None:
    """
    015 has a CHECK for this. Filtering here means an out-of-range value
    imports the project WITHOUT a TRL, rather than failing the batch —
    the project still says who is working on what.
    """
    assert parse_project(_detail(trlCurrent=trl))["trl_current"] == expected


def test_a_project_with_no_lead_organisation_still_parses() -> None:
    r = parse_project(_detail(leadOrganization=None))
    assert r is not None and r["org"] is None


@pytest.mark.parametrize("broken", [
    {"project": {"title": "no id"}},
    {"project": {"projectId": 1}},
    {"project": {}},
    {},
])
def test_a_detail_without_an_id_or_title_is_refused(broken) -> None:
    """Counted as unparseable and reported, not written as a blank row."""
    assert parse_project(broken) is None


# ── Found by the first real import, 2026-09-15 ───────────────────────

@pytest.mark.parametrize("raw,expected", [
    # lastUpdated, US short form with a two-digit year. 500 of 500
    # projects in the first import reported an unparseable date, and
    # this was all of them.
    ("01/27/25", date(2025, 1, 27)),
    ("08/20/26", date(2026, 8, 20)),
    ("12/31/99", date(1999, 12, 31)),
])
def test_the_us_short_date_techport_uses_for_lastupdated(raw, expected) -> None:
    """
    TechPort uses TWO date formats in one record: startDate and endDate
    are ISO, lastUpdated is MM/DD/YY. The MM/DD order is established
    rather than assumed - "01/27/25" has 27 in the second position, and
    27 is not a month.
    """
    assert _date(raw) == expected


@pytest.mark.parametrize("code", ["TX08.X", "TX11.X", "TX14.X", "TX08.1.X"])
def test_natures_other_buckets_are_valid_taxonomy(code: str) -> None:
    """
    The first import refused five of these as malformed. They are NASA's
    own "Other" buckets. Measured across 548 cached details: 518 strict,
    5 with `.X`, nothing else - so 017 relaxes the constraint to exactly
    what the source uses.
    """
    r = parse_project(_detail(primaryTaxonomyNodes=[
        {"code": code, "title": "Other Something"}]))
    assert [n["tx_code"] for n in r["nodes"]] == [code]
    assert r["bad_nodes"] == []


@pytest.mark.parametrize("bad", [
    "Sensors and Instruments", "tx08", "TX8", "11.6.4", "TXAB", "",
])
def test_relaxing_for_X_did_not_let_a_title_through(bad: str) -> None:
    """
    The whole point of the pattern is refusing a title in the code
    column (AD-086). Widening it for `X` must not widen it for prose.
    """
    r = parse_project(_detail(primaryTaxonomyNodes=[
        {"code": bad, "title": "a title"}]))
    assert r["nodes"] == []
