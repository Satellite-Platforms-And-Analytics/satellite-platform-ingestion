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


def test_apply_refuses_until_the_question_is_answered(capsys) -> None:
    rc = main(["--apply"])
    out = capsys.readouterr().out
    assert rc != 0, "--apply must not succeed by doing nothing"
    assert "leadOrganization" in out, (
        "the refusal must name what is unanswered, or it reads as a "
        "missing feature rather than a deliberate stop"
    )
    assert "--survey" in out, "it must say how to answer it"


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
