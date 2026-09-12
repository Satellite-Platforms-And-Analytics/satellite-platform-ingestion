"""
Tests for the launch-clustering behind new-object detection.

The international designator is what separates "a rideshare deployed
forty payloads" from "something that launched in 1975 just broke into
forty pieces". Both are forty new rows; only one is an event. So the
parsing has to be strict about what it accepts, and honest about what it
cannot attribute - an unparseable designator must NOT quietly become its
own launch, because that would turn every analyst object into a fake
"new launch" and bury the real signal.
"""
from __future__ import annotations

import pytest

from check_new_objects import launch_key


@pytest.mark.parametrize("intl,expected", [
    ("1998-067A",   "1998-067"),   # ISS
    ("1998-067ZZZ", "1998-067"),   # a much later piece of the same launch
    ("2026-123AB",  "2026-123"),
    ("1957-001A",   "1957-001"),
    ("  1998-067A ", "1998-067"),  # tolerate surrounding whitespace
])
def test_pieces_of_one_launch_share_a_key(intl, expected):
    assert launch_key(intl) == expected


def test_every_piece_of_a_launch_collapses_to_the_same_key():
    pieces = ["1998-067A", "1998-067B", "1998-067QP", "1998-067ZZZ"]
    assert len({launch_key(p) for p in pieces}) == 1


def test_different_launches_in_the_same_year_stay_separate():
    assert launch_key("2026-001A") != launch_key("2026-002A")


@pytest.mark.parametrize("bad", [
    None, "", "   ", "TBA", "UNKNOWN", "1998067A", "98-067A",
    "ABCD-067A", "1998-06AA", "1998x067A",
])
def test_unparseable_designators_return_none(bad):
    # None routes the object to the "cannot attribute" list. Returning a
    # truncated string instead would invent a launch that does not exist.
    assert launch_key(bad) is None


def test_a_short_designator_does_not_become_a_launch():
    # '2026-12' is 7 characters; accepting it would merge it with
    # anything else beginning the same way.
    assert launch_key("2026-12") is None


def test_analyst_style_identifiers_are_not_silently_grouped():
    # Uncatalogued and analyst objects often carry no real designator.
    # If these grouped together they would present as one enormous
    # "new launch" every single run.
    for value in ("TBA", "", None, "ANALYST"):
        assert launch_key(value) is None


# ── launch_year: the false-positive fix ───────────────────────────────
#
# Written against a real misclassification. On 2026-09-01 ingestion began
# fetching CelesTrak's debris groups; 587 COSMOS 2251 fragments and 111
# IRIDIUM 33 fragments arrived at once and were reported as NEW LAUNCHES
# because the catalogue had held none of them before. Their designators
# read 1993-036 and 1997-051.

from check_new_objects import launch_year, NEW_LAUNCH_MAX_AGE_YEARS


@pytest.mark.parametrize("key,expected", [
    ("2026-196", 2026),
    ("1993-036", 1993),   # COSMOS 2251 - the false positive
    ("1997-051", 1997),   # IRIDIUM 33
    ("1957-001", 1957),
])
def test_launch_year_reads_the_designator(key, expected):
    assert launch_year(key) == expected


@pytest.mark.parametrize("bad", [None, "", "abc", "ABCD-036", "202"])
def test_launch_year_is_none_when_unreadable(bad):
    assert launch_year(bad) is None


def test_a_1993_designator_is_never_a_new_launch_in_2026():
    # The classifier's rule: prior == 0 AND the launch is recent.
    # Without the second half, first-seen date alone reported a 2009
    # collision's debris as a 2026 launch.
    this_year = 2026
    assert launch_year("1993-036") < this_year - NEW_LAUNCH_MAX_AGE_YEARS
    assert launch_year("2026-196") >= this_year - NEW_LAUNCH_MAX_AGE_YEARS


def test_the_window_tolerates_a_late_catalogued_launch():
    # An object can be catalogued months after launch, and a launch late
    # in the previous year must still count as new.
    this_year = 2026
    assert launch_year("2025-276") >= this_year - NEW_LAUNCH_MAX_AGE_YEARS


# ── Object-type mix ───────────────────────────────────────────────────
#
# "What kind of material is this" is the question the user actually
# asked. object_type only became available on 2026-09-05, when SATCAT
# enrichment filled it for 96.8% of the catalogue.

from check_new_objects import _type_mix


def test_type_mix_counts_and_orders_by_frequency():
    rows = [(1, "a", None, "DEBRIS"), (2, "b", None, "PAYLOAD"),
            (3, "c", None, "DEBRIS"), (4, "d", None, "DEBRIS")]
    assert _type_mix(rows, 3) == "3 DEBRIS, 1 PAYLOAD"


def test_type_mix_names_missing_types_rather_than_dropping_them():
    # A row with no object_type is one SATCAT could not describe. It must
    # still be counted, or the totals stop matching the row count.
    rows = [(1, "a", None, None), (2, "b", None, "PAYLOAD")]
    out = _type_mix(rows, 3)
    assert "unknown" in out and "1 PAYLOAD" in out


def test_type_mix_totals_match_the_row_count():
    rows = [(i, "x", None, t) for i, t in
            enumerate(["PAYLOAD", "DEBRIS", None, "ROCKET BODY", "DEBRIS"])]
    total = sum(int(p.split()[0]) for p in _type_mix(rows, 3).split(", "))
    assert total == len(rows)


def _launches_query() -> str:
    import inspect
    import check_new_objects as m
    src = inspect.getsource(m.main)
    return src.split("NEW LAUNCHES")[0].split("launches = q(conn")[-1]


def test_new_launches_are_selected_by_launch_date_not_first_seen():
    # The substantive guard: the "NEW LAUNCHES" query must filter on when
    # the object came into existence. Filtering on created_at is what
    # reported 587 COSMOS 2251 fragments from 1993 as a new launch.
    q = _launches_query()
    assert "WHERE {EFFECTIVE_LAUNCH} >=" in q, (
        "new launches must be selected by the effective launch date")
    assert "created_at >=" not in q, (
        "created_at is when WE first saw a row, not when the object "
        "came into existence")


def test_new_launches_use_the_deployment_date_when_there_is_one():
    """
    launch_date alone misfiles every station-deployed object.

    The international designator convention gives an object released from
    a space station the STATION's designator, so SATCAT's launch_date for
    every ISS cubesat is 1998-11-20 - Zarya's launch. Measured against
    GCAT on 2026-09-12: 532 catalogued objects, 8 of them arriving in
    2026, each with an apparent age near 10,000 days. All of them fell
    past the `launch_age_days <= DEPLOYMENT_WINDOW_DAYS` test and were
    reported as `newly_visible` - an old object that merely became
    trackable - rather than as the deployment it was.

    This is the same failure as the 2026-203 defect on 2026-09-09: a
    detector keyed on launch_date fed a value that is correct by
    convention and useless for the question. There the value was NULL;
    here it is 1998.
    """
    import check_new_objects as m
    assert m.EFFECTIVE_LAUNCH == "COALESCE(deployment_date, launch_date)"
    q = _launches_query()
    assert "deployment_date" not in q or "{EFFECTIVE_LAUNCH}" in q, (
        "reference the shared expression rather than inlining the "
        "COALESCE, so every query that asks an object's age agrees")
    # No bare `launch_date >=` comparison may survive: that is the form
    # that reads 1998 for an object deployed last week.
    import re
    assert not re.search(r"(?<![_{])\blaunch_date\s*>=", q), (
        "a bare launch_date comparison is back; it reads 1998-11-20 for "
        "every ISS-deployed cubesat")


def test_the_designator_year_rule_survives_only_as_a_fallback():
    # Uncatalogued objects have no launch_date because they have no
    # catalogue entry, so launch_year still has a job — but it must not
    # be the primary test again.
    import check_new_objects as m
    assert m.NEW_LAUNCH_MAX_AGE_YEARS == 2
    assert m.launch_year("1993-036") == 1993
    assert "fallback" in (m.launch_year.__doc__ or "").lower()


# ── Why did these objects show up now? ────────────────────────────────
#
# Written against real output from 2026-09-05. The first classifier keyed
# only on "did we hold this launch before" and reported HULIANWANG DIGUI
# payloads — from a launch 32 days old — as fragmentation, because the
# launch fell outside the 30-day reporting window while its payloads
# arrived inside it. One window was answering two different questions.

from check_new_objects import (
    classify_arrival, DEPLOYMENT_WINDOW_DAYS, SAME_DAY_INGESTION_FRACTION)


def test_recent_payloads_are_deployment_not_fragmentation():
    # HULIANWANG DIGUI-185/186: launched 2026-08-04, 7 payloads arriving
    # 2026-08-08. A launch keeps being catalogued for weeks.
    assert classify_arrival(32, {"PAYLOAD"}, 7, 1.0) == "deployment"


def test_a_payload_just_outside_the_report_window_is_still_deployment():
    # The exact failure: 32 days old, reported with --days 30.
    for age in (31, 45, DEPLOYMENT_WINDOW_DAYS):
        assert classify_arrival(age, {"PAYLOAD"}, 2, 1.0) == "deployment"


def test_debris_under_an_old_launch_is_fragmentation():
    # 1982-092 — COSMOS 1408, the 2021 ASAT test, still shedding.
    assert classify_arrival(16_060, {"DEBRIS"}, 3, 1.0) == "fragmentation"


def test_rocket_body_pieces_count_as_fragmentation_too():
    assert classify_arrival(9_000, {"ROCKET BODY"}, 4, 0.5) == "fragmentation"


def test_debris_on_a_recent_launch_is_still_fragmentation():
    # A new launch shedding debris is an event, not deployment. The
    # deployment shortcut must not swallow it.
    assert classify_arrival(10, {"PAYLOAD", "DEBRIS"}, 5, 1.0) \
        == "fragmentation"


def test_a_bulk_same_day_debris_dump_is_an_ingestion_change():
    # 1993-036: 587 COSMOS 2251 fragments of a 2009 collision, all
    # appearing in one afternoon because a debris group was added.
    assert classify_arrival(12_000, {"DEBRIS"}, 587, 1.0) \
        == "ingestion_change"


def test_a_real_breakup_spread_over_days_stays_fragmentation():
    # Same size, but catalogued over time — which is what a genuine
    # break-up looks like. This is the distinction that keeps the alert
    # meaningful.
    assert classify_arrival(12_000, {"DEBRIS"}, 587, 0.4) == "fragmentation"


def test_the_same_day_threshold_is_not_off_by_one():
    just_under = SAME_DAY_INGESTION_FRACTION - 0.01
    assert classify_arrival(12_000, {"DEBRIS"}, 50, just_under) \
        == "fragmentation"
    assert classify_arrival(12_000, {"DEBRIS"}, 50,
                            SAME_DAY_INGESTION_FRACTION) == "ingestion_change"


def test_a_small_same_day_debris_group_is_not_dismissed_as_ingestion():
    # Three pieces arriving together is an event, not a config change.
    # Only bulk arrivals get the benefit of that doubt.
    assert classify_arrival(16_060, {"DEBRIS"}, 3, 1.0) == "fragmentation"


def test_old_payloads_are_newly_visible_not_an_event():
    # 2018-038 TESS: a payload from 2018 we simply had not held.
    assert classify_arrival(3_000, {"PAYLOAD"}, 1, 1.0) == "newly_visible"


def test_an_unknown_launch_date_does_not_become_deployment():
    # 1987-060: two analyst objects with a designator but no launch date.
    # Guessing "recent" for a missing date would hide them.
    assert classify_arrival(None, {"PAYLOAD"}, 2, 1.0) == "newly_visible"
    assert classify_arrival(None, {"DEBRIS"}, 2, 1.0) == "fragmentation"
