"""
Guards the WIT resource map against taxonomy drift.

Background: on 2026-08-30, 17 of 18 subdomains named in _RESOURCE_MAP did
not exist in the WIT database. Nothing failed - the precise query strategy
just returned nothing and the broad fallbacks filled the results with noise
(regulatory_sources was 84 entries, mostly NASA Earthdata and research
portals). The failure was invisible for weeks. These tests make it loud.

Skips cleanly when WIT or its database is unavailable, so it is safe in CI
where the WIT database is not present.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def resources():
    try:
        from src.resources import SatelliteResources
    except Exception as exc:                       # pragma: no cover
        pytest.skip(f"WIT not importable: {exc}")
    try:
        res = SatelliteResources()
    except Exception as exc:                       # pragma: no cover
        pytest.skip(f"WIT database unavailable: {exc}")
    if not res._load_taxonomy():                   # empty DB -> nothing to check
        res.close()
        pytest.skip("WIT taxonomy is empty")
    yield res
    res.close()


def test_every_configured_name_exists(resources):
    """Every domain and subdomain in _RESOURCE_MAP resolves in the taxonomy."""
    problems = resources.validate_taxonomy(strict=False)
    assert not problems, "stale names in _RESOURCE_MAP:\n" + "\n".join(
        f"  {cat}: {issue}" for cat, issues in problems.items() for issue in issues
    )


def test_strict_mode_raises_on_drift(resources):
    """A bad name must raise in strict mode, not degrade silently."""
    from src import resources as R

    category = "tle_sources"
    original = R._RESOURCE_MAP[category]["subdomains"]
    R._RESOURCE_MAP[category]["subdomains"] = ["Definitely Not A Real Subdomain"]
    try:
        with pytest.raises(ValueError, match="taxonomy validation failed"):
            resources.validate_taxonomy(strict=True)
    finally:
        R._RESOURCE_MAP[category]["subdomains"] = original


def test_results_are_deterministic(resources):
    """
    Same query, same rows, same order. Guards the LIMIT-without-ORDER-BY bug
    that made regulatory_sources return 84 on one machine and 77 on another.
    """
    for category in ("tle_sources", "regulatory_sources", "launch_providers"):
        resources._cache.clear()
        first = [s["url"] for s in resources._query(category)]
        resources._cache.clear()
        second = [s["url"] for s in resources._query(category)]
        assert first == second, f"{category} returned unstable results"


def test_acronyms_are_case_sensitive(resources):
    """
    'EAR' must not match 'Earth' or 'research'. SQLite LIKE is
    case-insensitive for ASCII; acronym terms therefore use GLOB.
    """
    assert resources._is_acronym("EAR")
    assert resources._is_acronym("ITAR")
    assert resources._is_acronym("TRL")
    assert not resources._is_acronym("licensing")
    assert not resources._is_acronym("remote sensing")

    names = " ".join((s.get("name") or "").lower()
                     for s in resources._query("regulatory_sources"))
    assert "earthdata" not in names, "case-insensitive acronym match has regressed"
