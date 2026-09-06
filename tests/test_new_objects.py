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
