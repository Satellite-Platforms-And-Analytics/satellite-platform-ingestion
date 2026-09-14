"""
One row per object, not one row per GCAT record.

GCAT's primary key is JCAT, not Satcat. On the 2026-09-14 file, 1,944
NORAD catalogue numbers carry more than one GCAT record - 3,735 extra rows
- because NORAD issues one number to a payload while GCAT tracks the
payload, its rocket stages and attached pieces separately:

    norad 41847   S41847  KCHT    KS-1Q                 <- the satellite
                  A08476  CASC    CZ-11 Y2 Stage 4      <- a rocket stage
                  A08477  DFHZ    Fengtai shaonian yi hao

Every one of those rows was being sent to `UPDATE ... FROM (VALUES %s)`,
where Postgres picks an arbitrary matching row when the join is ambiguous.
So for up to 1,944 objects the operator written was whichever record
Postgres reached first - and 41847 was stored as CASC, owner of the fourth
stage of the launch vehicle, rather than KCHT who owns the satellite.

It produced no error and no warning. Those columns had no prior value, so
the survey counted them as GAINS.

This file is the regression guard. It uses a handful of synthetic records
rather than the 14 MB catalogue, so it runs offline in milliseconds and
states the rule rather than re-measuring it.
"""
from __future__ import annotations

from collections import Counter

from src.catalog.seed_gcat import _one_row_per_object


def rec(norad: int, jcat: str, operator: str) -> dict:
    return {"norad_id": norad, "_gcat_jcat": jcat, "operator": operator}


def test_the_satellite_record_wins_over_rocket_stages():
    rows = [
        rec(41847, "A08476", "CASC"),     # a stage, listed first on purpose
        rec(41847, "S41847", "KCHT"),     # the satellite
        rec(41847, "A08477", "DFHZ"),
    ]
    out = _one_row_per_object(rows, Counter())
    assert len(out) == 1
    assert out[0]["operator"] == "KCHT", (
        "the S record is the satellite; an A record is a rocket stage or an "
        "attached piece, and writing its owner onto the payload is the "
        "defect this rule exists to prevent")


def test_order_does_not_decide_the_answer():
    """
    The original defect was order-dependence -- Postgres picking whichever
    ambiguous row it reached. A rule that still depends on input order has
    not fixed anything.
    """
    group = [rec(1, "A1", "stage"), rec(1, "S1", "sat"), rec(1, "A2", "piece")]
    first = _one_row_per_object(list(group), Counter())[0]["operator"]
    last = _one_row_per_object(list(reversed(group)), Counter())[0]["operator"]
    assert first == last == "sat"


def test_a_single_record_is_untouched_whatever_its_prefix():
    out = _one_row_per_object([rec(7, "A999", "only")], Counter())
    assert len(out) == 1 and out[0]["operator"] == "only"


def test_counts_are_reported_not_swallowed():
    """
    A dedupe that drops thousands of rows without saying so is
    indistinguishable from a parser bug.
    """
    st = Counter()
    _one_row_per_object(
        [rec(1, "S1", "a"), rec(1, "A1", "b"), rec(2, "S2", "c")], st)
    assert st["ids_with_multiple_gcat_records"] == 1
    assert st["resolved_by_S_record"] == 1


def test_no_satellite_record_is_flagged_rather_than_guessed():
    st = Counter()
    out = _one_row_per_object([rec(5, "A1", "x"), rec(5, "A2", "y")], st)
    assert len(out) == 1
    assert st["ids_with_no_S_record"] == 1, (
        "keeping a row is fine; keeping it silently is not")


def test_several_satellite_records_is_flagged():
    """
    Measured impossible on the 2026-09-14 file - all 1,944 duplicated ids
    have exactly one S record. 'Impossible today' is a property of today's
    file, so it surfaces as a number instead of an arbitrary pick.
    """
    st = Counter()
    out = _one_row_per_object([rec(9, "S1", "x"), rec(9, "S2", "y")], st)
    assert len(out) == 1
    assert st["ids_with_several_S_records"] == 1


def test_every_output_id_is_unique():
    rows = [rec(i // 2, f"S{i}" if i % 2 else f"A{i}", "x") for i in range(20)]
    out = _one_row_per_object(rows, Counter())
    assert len({r["norad_id"] for r in out}) == len(out)
