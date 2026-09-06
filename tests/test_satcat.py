"""
Tests for the SATCAT enrichment source.

Everything here runs on a constructed CSV, not the network. The point is
the translation layer: SATCAT's coded vocabularies into the schema's, and
the guards that are supposed to fire when the file changes shape.

The lesson these are written against: the future-epoch guard that broke
every TLE write for three days had a test, and the test passed, because
it was built from an assumption about the feed rather than the feed. So
the header fixture below is the documented SATCAT header verbatim, and
`test_expected_header_matches_the_documented_field_order` exists to make
a silent edit to EXPECTED_HEADER fail rather than pass.
"""
from __future__ import annotations

import pytest

from src.catalog.seed_satcat import (
    EXPECTED_HEADER,
    OBJECT_TYPE_MAP,
    OPS_STATUS_MAP,
    OWNER_TO_ISO,
    parse,
    rcs_bucket,
    to_row,
)

HEADER = ",".join(EXPECTED_HEADER)


def csv_of(*rows: str) -> str:
    return HEADER + "\n" + "\n".join(rows) + "\n"


# OBJECT_NAME,OBJECT_ID,NORAD_CAT_ID,OBJECT_TYPE,OPS_STATUS_CODE,OWNER,
# LAUNCH_DATE,LAUNCH_SITE,DECAY_DATE,PERIOD,INCLINATION,APOGEE,PERIGEE,
# RCS,DATA_STATUS_CODE,ORBIT_CENTER,ORBIT_TYPE
VANGUARD = ("VANGUARD 1,1958-002B,00005,PAY,-,US,1958-03-17,AFETR,,"
            "132.66,34.24,3841,649,0.1222,,EARTH,ORB")
ISS = ("ISS (ZARYA),1998-067A,25544,PAY,+,CIS,1998-11-20,TTMTR,,"
       "92.83,51.64,421,412,399.05,,EARTH,ORB")
DEBRIS = ("THOR ABLESTAR DEB,1960-004C,00047,DEB,?,US,1960-06-22,AFETR,,"
          "104.32,66.66,999,555,0.0339,,EARTH,ORB")


# ── The shape guards ──────────────────────────────────────────────────

def test_expected_header_matches_the_documented_field_order():
    # Seventeen fields, in CelesTrak's documented order. If someone
    # reorders EXPECTED_HEADER to make a failing parse "work", every
    # field below it is read from the wrong column - so pin it.
    assert EXPECTED_HEADER[:6] == [
        "OBJECT_NAME", "OBJECT_ID", "NORAD_CAT_ID", "OBJECT_TYPE",
        "OPS_STATUS_CODE", "OWNER"]
    assert EXPECTED_HEADER[-4:] == [
        "RCS", "DATA_STATUS_CODE", "ORBIT_CENTER", "ORBIT_TYPE"]
    assert len(EXPECTED_HEADER) == 17


def test_a_changed_column_layout_raises_rather_than_shifting_fields():
    shifted = EXPECTED_HEADER.copy()
    shifted.insert(3, "NEW_UPSTREAM_COLUMN")
    text = ",".join(shifted) + "\n"
    with pytest.raises(RuntimeError, match="column layout has changed"):
        parse(text)


def test_parse_reads_records_by_name():
    rows = parse(csv_of(VANGUARD, ISS))
    assert len(rows) == 2
    assert rows[1]["OBJECT_NAME"] == "ISS (ZARYA)"
    assert rows[1]["NORAD_CAT_ID"] == "25544"
    assert rows[1]["OWNER"] == "CIS"


# ── The vocabularies ──────────────────────────────────────────────────

def test_status_follows_celestraks_own_definition_of_active():
    # celestrak.org/satcat/status.php: "Active is any satellite with an
    # operational status of +, P, B, S, or X."
    for code in "+PBSX":
        assert OPS_STATUS_MAP[code] == "ACTIVE"
    assert OPS_STATUS_MAP["-"] == "INACTIVE"
    assert OPS_STATUS_MAP["D"] == "DECAYED"


def test_an_absent_status_is_null_not_unknown():
    # '?' means the source says it does not know. '' means the source
    # said nothing. Collapsing them would invent information.
    assert OPS_STATUS_MAP["?"] == "UNKNOWN"
    assert OPS_STATUS_MAP[""] is None


def test_mapped_values_stay_inside_the_schemas_vocabulary():
    # status is VARCHAR(20), object_type VARCHAR(20), with the
    # vocabularies named in 001_core_schema.sql's comments.
    assert set(v for v in OPS_STATUS_MAP.values() if v) <= {
        "ACTIVE", "INACTIVE", "DECAYED", "UNKNOWN"}
    assert set(v for v in OBJECT_TYPE_MAP.values() if v) <= {
        "PAYLOAD", "ROCKET BODY", "DEBRIS", "UNKNOWN"}
    for value in OPS_STATUS_MAP.values():
        assert value is None or len(value) <= 20
    for value in OBJECT_TYPE_MAP.values():
        assert value is None or len(value) <= 20


def test_an_unmapped_code_raises_instead_of_defaulting():
    # The whole reason this module surveys before it applies. A new
    # OBJECT_TYPE must stop the run, not become UNKNOWN.
    row = parse(csv_of(VANGUARD))[0]
    row["OBJECT_TYPE"] = "TBA"
    with pytest.raises(KeyError, match="unmapped OBJECT_TYPE"):
        to_row(row)

    row = parse(csv_of(VANGUARD))[0]
    row["OPS_STATUS_CODE"] = "Z"
    with pytest.raises(KeyError, match="unmapped OPS_STATUS_CODE"):
        to_row(row)


# ── RCS bucketing ─────────────────────────────────────────────────────

@pytest.mark.parametrize("m2,expected", [
    ("0.0339", "SMALL"),
    ("0.0999", "SMALL"),
    ("0.1", "MEDIUM"),
    ("1.0", "MEDIUM"),
    ("1.0001", "LARGE"),
    ("399.05", "LARGE"),
])
def test_rcs_buckets_at_the_spacetrack_thresholds(m2, expected):
    assert rcs_bucket(m2) == expected


@pytest.mark.parametrize("value", ["", "   ", "N/A", "not a number"])
def test_unparseable_rcs_is_null_not_small(value):
    # Defaulting to SMALL would silently assert a measurement nobody made.
    assert rcs_bucket(value) is None


def test_bucket_boundaries_do_not_leave_a_gap():
    # 0.1 and 1.0 exactly must each land in exactly one bucket.
    assert rcs_bucket("0.1") == "MEDIUM"
    assert rcs_bucket("1.0") == "MEDIUM"


# ── Owner resolution ──────────────────────────────────────────────────

def test_owner_code_is_kept_verbatim_even_when_it_does_not_resolve():
    row = to_row(parse(csv_of(ISS))[0])
    assert row["owner_code"] == "CIS"
    assert row["country_code"] is None, (
        "CIS denotes the former Soviet Union; collapsing it to RUS "
        "asserts a succession this project has no basis to assert")


def test_a_resolvable_owner_fills_country_code():
    row = to_row(parse(csv_of(VANGUARD))[0])
    assert row["owner_code"] == "US"
    assert row["country_code"] == "USA"


def test_every_iso_target_fits_the_foreign_key_column():
    # country_code is VARCHAR(3) REFERENCES countries(code), and
    # countries holds ISO 3166-1 alpha-3.
    for satcat_code, iso in OWNER_TO_ISO.items():
        assert len(iso) == 3, f"{satcat_code} -> {iso} will not fit"
        assert iso.isupper()


# ── The row the writer will receive ───────────────────────────────────

def test_row_carries_provenance_for_every_record():
    for line in (VANGUARD, ISS, DEBRIS):
        row = to_row(parse(csv_of(line))[0])
        assert row["data_source"] == "celestrak_satcat"
        assert row["match_method"] == "norad_id"
        assert row["source_confidence"] == 1.0
        assert row["matched_at"] is not None


def test_numeric_columns_arrive_as_numbers():
    row = to_row(parse(csv_of(ISS))[0])
    assert row["period_min"] == pytest.approx(92.83)
    assert row["inclination_deg"] == pytest.approx(51.64)
    assert row["apogee_km"] == pytest.approx(421.0)
    assert row["perigee_km"] == pytest.approx(412.0)


def test_orbit_type_is_never_written():
    # SATCAT's ORBIT_TYPE describes disposition (ORB/LAN/IMP/DOC); the
    # schema's orbit_type means orbital geometry (polar, sun-sync).
    # Same name, different meaning - see the module docstring.
    row = to_row(parse(csv_of(VANGUARD))[0])
    assert "orbit_type" not in row


def test_a_record_without_a_catalogue_number_is_skipped_not_guessed():
    row = parse(csv_of(VANGUARD))[0]
    row["NORAD_CAT_ID"] = ""
    assert to_row(row) is None


def test_decayed_objects_still_produce_a_row():
    # They are part of the catalogue's history and Phase 3 will want them
    # in launch-rate analysis; only their status differs.
    row = parse(csv_of(VANGUARD))[0]
    row["OPS_STATUS_CODE"] = "D"
    built = to_row(row)
    assert built["status"] == "DECAYED"
    assert built["launch_date"] == "1958-03-17"


# ── The fetch guard ───────────────────────────────────────────────────
#
# The first version of this refused when the fetch log was fresh but no
# cached copy existed, leaving --force as the only way forward. That is
# backwards: the guard exists to stop us re-requesting data we already
# hold, and a missing cache means we do not hold it. A guard whose only
# escape hatch is overriding it teaches people to override guards.

from src.catalog.seed_satcat import fetch_decision, MIN_FETCH_INTERVAL_S

FRESH = MIN_FETCH_INTERVAL_S - 1
STALE = MIN_FETCH_INTERVAL_S + 1


def test_fresh_and_cached_reuses_the_file():
    assert fetch_decision(FRESH, cache_exists=True, force=False) == "cache"


def test_fresh_but_not_cached_fetches_rather_than_demanding_force():
    assert fetch_decision(FRESH, cache_exists=False,
                          force=False) == "fetch_uncached"


def test_stale_always_fetches():
    assert fetch_decision(STALE, cache_exists=True, force=False) == "fetch"
    assert fetch_decision(STALE, cache_exists=False, force=False) == "fetch"


def test_never_fetched_fetches():
    assert fetch_decision(None, cache_exists=False, force=False) == "fetch"


def test_force_overrides_a_usable_cache():
    assert fetch_decision(FRESH, cache_exists=True, force=True) == "fetch"


def test_the_window_boundary_is_not_off_by_one():
    assert fetch_decision(MIN_FETCH_INTERVAL_S, True, False) == "fetch"
    assert fetch_decision(MIN_FETCH_INTERVAL_S - 1, True, False) == "cache"


def test_no_input_state_ever_leaves_force_as_the_only_option():
    # Exhaustive over the decision's inputs: every combination must
    # produce a usable outcome without the caller reaching for --force.
    for age in (None, FRESH, STALE):
        for cached in (True, False):
            assert fetch_decision(age, cached, force=False) in {
                "fetch", "cache", "fetch_uncached"}
