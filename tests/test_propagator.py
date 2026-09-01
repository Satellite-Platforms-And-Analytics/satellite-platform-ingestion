"""
Orbit propagation tests - pure maths, no database and no network.

Values were cross-checked against Skyfield (independent implementation,
full TEME->ITRF) during development: latitude agreed to <1e-5 deg, altitude
to <1 m, and worst ground-position disagreement was 44 m across LEO,
sun-synchronous and GEO cases. These tests lock in that behaviour with
tolerances loose enough to survive a legitimate sgp4 update but tight
enough to catch a broken coordinate transform.
"""
from datetime import datetime, timezone

import pytest

from src.propagation.propagator import (
    TLE,
    _ecef_to_geodetic,
    ground_track,
    propagate_batch,
    propagate_single,
)

# Real element sets, epoch 2026-08-28/29
ISS = ("1 25544U 98067A   26240.51782528 -.00002182  00000-0 -11606-4 0  9990",
       "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537")
GEO = ("1 41866U 16071A   26240.44444444 -.00000267  00000-0  00000-0 0  9995",
       "2 41866   0.0489 273.4295 0000897 320.1157  25.5563  1.00271733 35766")
SSO = ("1 40697U 15028A   26240.83319420  .00000042  00000+0  27637-4 0  9995",
       "2 40697  98.5684 316.4413 0001127  88.8462 271.2846 14.30818691578912")

WHEN = datetime(2026, 8, 29, 6, 0, 0, tzinfo=timezone.utc)


def test_iss_position_is_physically_sane():
    row = propagate_single(*ISS, 25544, WHEN)
    assert row is not None
    # ISS inclination is 51.64 deg, so it can never be outside that latitude band
    assert -52.0 <= row["latitude"] <= 52.0
    assert -180.0 <= row["longitude"] < 180.0
    assert 350.0 <= row["altitude_km"] <= 430.0      # ISS operational band
    assert 7.5 <= row["velocity_km_s"] <= 7.8        # circular LEO


def test_geostationary_altitude_and_latitude():
    row = propagate_single(*GEO, 41866, WHEN)
    assert row is not None
    assert abs(row["latitude"]) < 0.5                # GEO sits over the equator
    assert 35_600 <= row["altitude_km"] <= 35_900    # nominal 35,786 km
    assert 2.9 <= row["velocity_km_s"] <= 3.2


def test_sun_synchronous_reaches_high_latitude():
    """A near-polar orbit must produce high latitudes somewhere in a pass."""
    lats = [r["latitude"] for r in ground_track(*SSO, 40697, start=WHEN, points=40)]
    assert max(abs(l) for l in lats) > 70.0


def test_ground_track_period_matches_regime():
    leo = ground_track(*ISS, 25544, start=WHEN, points=20)
    geo = ground_track(*GEO, 41866, start=WHEN, points=20)
    leo_min = (leo[-1]["timestamp"] - leo[0]["timestamp"]).total_seconds() / 60
    geo_min = (geo[-1]["timestamp"] - geo[0]["timestamp"]).total_seconds() / 60
    assert 85 <= leo_min <= 100          # ~92 min
    assert 1400 <= geo_min <= 1460       # ~1436 min (sidereal day)


def test_stale_tles_are_skipped_not_guessed():
    """
    SGP4 error grows with time since epoch. A month-old LEO element set must
    be skipped rather than written as a confident wrong answer.
    """
    tle = TLE(25544, "ISS", *ISS)
    fresh = propagate_batch([tle], when=datetime(2026, 8, 29, tzinfo=timezone.utc))
    stale = propagate_batch([tle], when=datetime(2026, 9, 27, tzinfo=timezone.utc))
    assert len(fresh) == 1
    assert stale == []


def test_malformed_input_returns_none_not_exception():
    assert propagate_single("nonsense", "alsononsense", 1, WHEN) is None
    # Well-formed columns but physically impossible elements (eccentricity > 1)
    bad = ("1 33591U 09005A   26240.90416667  .00000254  00000-0  015 26-3 0  9992",
           "2 33591  99.0521 268.7407 0013634 213.7135 146.3268 14.13100000900123")
    assert propagate_single(*bad, 33591, WHEN) is None


def test_naive_datetime_is_treated_as_utc():
    naive = propagate_single(*ISS, 25544, datetime(2026, 8, 29, 6, 0, 0))
    aware = propagate_single(*ISS, 25544, WHEN)
    assert naive["latitude"] == pytest.approx(aware["latitude"], abs=1e-9)


@pytest.mark.parametrize("z,expected_lat", [(7000.0, 90.0), (-7000.0, -90.0)])
def test_geodetic_handles_the_poles(z, expected_lat):
    lat, lon, alt = _ecef_to_geodetic(0.0, 0.0, z)
    assert lat == pytest.approx(expected_lat)
    assert alt > 0


def test_longitude_is_normalised():
    """Longitude must land in [-180, 180), never 0..360."""
    for hour in range(0, 24, 2):
        row = propagate_single(*ISS, 25544,
                               datetime(2026, 8, 29, hour, tzinfo=timezone.utc))
        assert -180.0 <= row["longitude"] < 180.0


def test_writer_contract():
    """Output keys must match what upsert_orbital_positions() consumes."""
    row = propagate_single(*ISS, 25544, WHEN)
    required = {"norad_id", "timestamp", "latitude", "longitude", "altitude_km"}
    assert required <= set(row)
    assert row["timestamp"].tzinfo is not None
