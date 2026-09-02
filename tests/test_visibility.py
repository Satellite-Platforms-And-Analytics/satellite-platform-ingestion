"""
Tests for src/visibility/compute.py.

These run without a database. The sensor is built by hand rather than
loaded, and the TLEs are literals, so the whole file is offline and safe
in CI.

The important test here is test_matches_independent_skyfield_calculation:
it recomputes altitude/azimuth/range from scratch, the way
src/tracking/main.py does, and compares. A test that only checked
compute_visibility against itself would pass no matter what the module
did - that mistake was already made once in tests/test_writer_columns.py
and rewritten.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from src.propagation.propagator import TLE
from src.visibility.compute import (
    MAX_ERROR_FRACTION,
    Sensor,
    _bin_label,
    _classify_regime,
    compute_visibility,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────────────

# AN/FPS-85, Eglin AFB. Values match the `sensors` row this job reads.
FPS85 = Sensor(
    id=1, short_name="FPS85", name="AN/FPS-85 Eglin",
    latitude=30.5725, longitude=-86.2147, elevation_m=45.0,
    min_elevation_deg=3.0, max_elevation_deg=90.0,
    boresight_azimuth_deg=180.0, azimuth_half_width_deg=60.0,
    apply_field_of_regard=True,
)

FULL_SKY = Sensor(
    id=2, short_name="TESTALL", name="Full sky test site",
    latitude=30.5725, longitude=-86.2147, elevation_m=45.0,
    min_elevation_deg=0.0, max_elevation_deg=90.0,
    boresight_azimuth_deg=None, azimuth_half_width_deg=None,
    apply_field_of_regard=False,
)


def _fresh_tle() -> TLE:
    """
    An ISS element set, re-epoched to yesterday so the 14-day staleness
    guard never rejects it and the test does not rot.

    Only the epoch field (columns 19-32 of line 1) is rewritten, and both
    line checksums are recomputed, so sgp4 accepts it.
    """
    line1 = ("1 25544U 98067A   24001.50000000  .00016717  00000-0  30777-3 0  9993")
    line2 = ("2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49814468 20000")

    y = datetime.now(timezone.utc) - timedelta(days=1)
    day_of_year = y.timetuple().tm_yday
    frac = (y.hour * 3600 + y.minute * 60 + y.second) / 86400.0
    epoch = f"{y.year % 100:02d}{day_of_year:03d}.{int(frac * 1e8):08d}"
    assert len(epoch) == 14
    line1 = line1[:18] + epoch + line1[32:]

    return TLE(norad_id=25544, name="ISS (ZARYA)",
               line1=_checksum(line1), line2=_checksum(line2))


def _checksum(line: str) -> str:
    """TLE mod-10 checksum over the first 68 columns; '-' counts as 1."""
    body = line[:68]
    total = sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in body)
    return body + str(total % 10)


# ─────────────────────────────────────────────────────────────────────────────
#  Bin labels - the string db.writer.insert_visibility_windows parses
# ─────────────────────────────────────────────────────────────────────────────

def test_bin_label_format_is_what_the_writer_parses():
    start = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)
    assert _bin_label(start, 1) == "0600-0700Z"


def test_bin_label_hour_survives_the_writers_parser():
    """
    insert_visibility_windows takes hour_bin as int(label[:2]). Every label
    this module can emit for a 1-hour bin must survive that unchanged.
    """
    midnight = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)
    for h in range(24):
        label = _bin_label(midnight + timedelta(hours=h), 1)
        assert int(label.split("-")[0][:2]) == h


def test_bin_label_wraps_at_midnight():
    start = datetime(2026, 9, 2, 23, 0, tzinfo=timezone.utc)
    assert _bin_label(start, 1) == "2300-0000Z"


# ─────────────────────────────────────────────────────────────────────────────
#  Regime classification
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rev_per_day,expected", [
    (15.5, "LEO"),     # ISS, ~93 min
    (11.30, "LEO"),    # ~127.4 min, just inside the LEO/MEO boundary
    (11.25, "MEO"),    # exactly 128.0 min, just outside it
    (2.0, "MEO"),      # ~720 min, GPS-like
    (1.0027, "GEO"),   # sidereal day
    (0.5, "HEO"),      # ~2880 min, Molniya-like apogee dwell
    (0.0, "UNKNOWN"),
])
def test_classify_regime(rev_per_day, expected):
    assert _classify_regime(rev_per_day) == expected


# ─────────────────────────────────────────────────────────────────────────────
#  Row shape - must match what insert_visibility_windows reads
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_KEYS = {
    "Hour Window", "Target Name", "Target Orbit", "Target NORAD",
    "Elevation (deg)", "Azimuth (deg)", "Range (km)",
}


def test_rows_carry_every_key_the_writer_reads():
    """
    Read the keys straight out of db.writer.insert_visibility_windows'
    source rather than restating them here, so renaming a key in the
    writer fails this test instead of silently passing.
    """
    import inspect
    import re

    from src.db import writer

    src = inspect.getsource(writer.insert_visibility_windows)
    read_keys = set(re.findall(r'r\.get\("([^"]+)"\)', src))
    read_keys |= set(re.findall(r'r\["([^"]+)"\]', src))
    read_keys.discard("confidence_score")      # documented as optional

    rows = compute_visibility(FULL_SKY, [_fresh_tle()], date.today())
    assert rows, "expected the ISS to be visible somewhere in a full day"
    assert read_keys <= set(rows[0]), (
        f"writer reads {sorted(read_keys - set(rows[0]))} which "
        f"compute_visibility does not emit")


def test_row_values_have_usable_types():
    rows = compute_visibility(FULL_SKY, [_fresh_tle()], date.today())
    r = rows[0]
    assert isinstance(r["Target NORAD"], int)
    assert isinstance(r["Elevation (deg)"], float)
    assert 0.0 <= r["Azimuth (deg)"] <= 360.0
    assert r["Range (km)"] > 0


def test_one_row_per_satellite_per_bin_at_most():
    rows = compute_visibility(FULL_SKY, [_fresh_tle()], date.today())
    seen = [(r["Target NORAD"], r["Hour Window"]) for r in rows]
    assert len(seen) == len(set(seen))


# ─────────────────────────────────────────────────────────────────────────────
#  Field of regard actually narrows the result
# ─────────────────────────────────────────────────────────────────────────────

def test_field_of_regard_is_a_strict_subset_of_full_sky():
    """
    FPS85 sweeps 180+-60 deg above 3 deg elevation. Every window it sees
    must also be seen by a co-located full-sky sensor, and it must see
    strictly fewer - otherwise the mask is not being applied.
    """
    tle = [_fresh_tle()]
    today = date.today()
    wide = compute_visibility(FULL_SKY, tle, today)
    narrow = compute_visibility(FPS85, tle, today)

    wide_bins = {r["Hour Window"] for r in wide}
    narrow_bins = {r["Hour Window"] for r in narrow}
    assert narrow_bins <= wide_bins
    assert len(narrow) < len(wide)


def test_every_windowed_sample_obeys_the_sensor_limits():
    rows = compute_visibility(FPS85, [_fresh_tle()], date.today())
    for r in rows:
        assert r["Elevation (deg)"] >= FPS85.min_elevation_deg
        assert r["Elevation (deg)"] <= FPS85.max_elevation_deg
        off_boresight = abs(
            ((r["Azimuth (deg)"] - FPS85.boresight_azimuth_deg + 180) % 360) - 180)
        assert off_boresight <= FPS85.azimuth_half_width_deg + 1e-9


# ─────────────────────────────────────────────────────────────────────────────
#  Independent recomputation - the test that would catch a real error
# ─────────────────────────────────────────────────────────────────────────────

def test_matches_independent_skyfield_calculation():
    """
    Rebuild one reported row from scratch the way src/tracking/main.py
    does - EarthSatellite minus a wgs84 topos, .altaz() - and require the
    numbers to agree. This is what makes the suite worth running: it
    compares against an outside calculation, not against itself.
    """
    from skyfield.api import EarthSatellite, load, wgs84

    tle = _fresh_tle()
    today = date.today()
    rows = compute_visibility(FULL_SKY, [tle], today, step_minutes=5)
    assert rows

    ts = load.timescale(builtin=True)
    sat = EarthSatellite(tle.line1, tle.line2, tle.name, ts)
    observer = wgs84.latlon(FULL_SKY.latitude, FULL_SKY.longitude,
                            elevation_m=FULL_SKY.elevation_m)

    row = rows[0]
    hour = int(row["Hour Window"][:2])

    # The reported sample is the first visible 5-minute step in the bin.
    # Walk the same grid and find it, then compare all three numbers.
    match = None
    for minute in range(0, 60, 5):
        t = ts.utc(today.year, today.month, today.day, hour, minute, 0)
        alt, az, dist = (sat - observer).at(t).altaz()
        if alt.degrees >= FULL_SKY.min_elevation_deg:
            match = (alt.degrees, az.degrees, dist.km)
            break

    assert match is not None, "independent pass found no visible sample"
    assert math.isclose(match[0], row["Elevation (deg)"], abs_tol=0.01)
    assert math.isclose(match[1], row["Azimuth (deg)"], abs_tol=0.01)
    assert math.isclose(match[2], row["Range (km)"], abs_tol=0.1)


# ─────────────────────────────────────────────────────────────────────────────
#  Failure handling
# ─────────────────────────────────────────────────────────────────────────────

STALE = TLE(
    norad_id=25544, name="ISS (STALE)",
    line1="1 25544U 98067A   24001.50000000  .00016717  00000-0  30777-3 0  9993",
    line2="2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49814468 20000",
)


def test_stale_tles_are_skipped_not_propagated():
    """A 2024-epoch element set is >14 days old and must be dropped."""
    rows = compute_visibility(FULL_SKY, [_fresh_tle(), STALE], date.today())
    assert {r["Target Name"] for r in rows} == {"ISS (ZARYA)"}


def test_an_entirely_stale_catalogue_raises():
    """
    If every element set is too old the sky is not empty - the fetcher has
    stopped. Returning [] here would write nothing and exit 0.
    """
    with pytest.raises(RuntimeError, match="older than"):
        compute_visibility(FULL_SKY, [STALE], date.today())


def test_prose_is_not_mistaken_for_a_stale_satellite():
    """
    sgp4 parses garbage into a 1949 epoch. Without a structural check that
    lands in the "stale" bucket and looks like ordinary old data.
    """
    from src.visibility.compute import _check_tle_lines

    with pytest.raises(ValueError):
        _check_tle_lines("not a tle", "also not")


def test_a_broken_batch_raises_instead_of_returning_empty():
    """
    The failure this project keeps hitting: everything fails, nothing is
    written, and the job exits 0. A batch that is entirely unparseable
    must raise, not return [].
    """
    junk = [TLE(norad_id=1, name="JUNK", line1="not a tle", line2="also not")]
    with pytest.raises(RuntimeError, match="defect in the computation"):
        compute_visibility(FULL_SKY, junk, date.today())


def test_a_few_bad_element_sets_do_not_stop_the_run():
    """One bad row in a batch is data, not a defect: keep going."""
    good = [_fresh_tle() for _ in range(40)]
    junk = TLE(norad_id=1, name="JUNK", line1="not a tle", line2="also not")
    rows = compute_visibility(FULL_SKY, good + [junk], date.today())
    assert rows
    assert 1 / 41 <= MAX_ERROR_FRACTION


def test_end_before_start_is_rejected():
    with pytest.raises(ValueError, match="not after"):
        compute_visibility(FULL_SKY, [_fresh_tle()], date.today(),
                           start_hhmm="1200", end_hhmm="0600")
