"""
Every Space-Track cache and ledger path must resolve under TLE_DATA_DIR.

WHY THIS TEST EXISTS
====================
Until 2026-09-05 the Satellite Visibility Tool and this repository's
src/tracking/ copy declared their cache paths differently: the tool used
`BASE_DIR/data/*.db`, this copy used `TLE_DATA_DIR/*.sqlite3`. Same
code, same account, two ledgers - and `api_request_log.py`'s docstring
describes exactly what that permits: two callers each see a stale cache
and two requests go out where the policy allows one. The ledger recorded
seven SATCAT requests on 2026-08-06 against a documented 1/day limit.

The failure was invisible because both files were individually correct.
Nothing compared them. This test is that comparison, for the half that
lives in this repository.

It parses config.py rather than importing it, so it runs in CI without
Space-Track credentials, without the caches, and without the optional
dependencies the tracking modules pull in.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[1] / "src" / "tracking" / "config.py"

#: Anything holding Space-Track state. A path here that is not built from
#: TLE_DATA_DIR is a second ledger waiting to happen.
ACCOUNT_STATE_CONSTANTS = [
    "API_REQUEST_LOG_DB",
    "SPACETRACK_BUDGET_DB",
    "TLE_HISTORY_CACHE_DB",
    "GP_HISTORY_CACHE_DB",
    "SATCAT_CACHE_DB",
    "CATALOG_CACHE_DB",
    "SATELLITE_CONFIDENCE_DB",
]

ASSIGN = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*os\.path\.join\(\s*(?P<root>[A-Za-z_][A-Za-z0-9_]*)\s*,",
    re.M)


@pytest.fixture(scope="module")
def assignments() -> dict:
    text = CONFIG.read_text(encoding="utf-8")
    return {m.group("name"): m.group("root") for m in ASSIGN.finditer(text)}


def test_config_exists():
    assert CONFIG.exists(), f"{CONFIG} is missing"


@pytest.mark.parametrize("name", ACCOUNT_STATE_CONSTANTS)
def test_account_state_lives_under_tle_data_dir(assignments, name):
    assert name in assignments, (
        f"{name} is not declared in src/tracking/config.py. If it moved, "
        f"update this test deliberately - do not delete the entry, because "
        f"the missing half is what caused the split ledger.")
    assert assignments[name] == "TLE_DATA_DIR", (
        f"{name} is built from {assignments[name]}, not TLE_DATA_DIR. "
        f"A Space-Track cache anchored to anything else gives this copy a "
        f"private ledger the other copy cannot see.")


def test_tle_data_dir_is_environment_overridable():
    # Both copies must be able to point at ONE folder. A hardcoded path
    # here would make agreement impossible.
    text = CONFIG.read_text(encoding="utf-8")
    assert 'os.environ.get("TLE_DATA_DIR")' in text or \
           "os.environ.get('TLE_DATA_DIR')" in text, (
        "TLE_DATA_DIR must be settable from the environment so the tool "
        "and the pipeline can be pointed at the same folder.")


def test_the_two_history_caches_are_not_the_same_file(assignments):
    # tle_history_cache.py stores `tle_elements` + `coverage`;
    # tle_cache.py stores `tle_history` BLOBs. The tool had these two
    # crossed, so one name pointed at the other module's schema.
    text = CONFIG.read_text(encoding="utf-8")
    def filename(const):
        m = re.search(rf'{const}\s*=\s*os\.path\.join\([^,]+,\s*"([^"]+)"', text)
        return m.group(1) if m else None
    tle = filename("TLE_HISTORY_CACHE_DB")
    gp = filename("GP_HISTORY_CACHE_DB")
    assert tle and gp, "both history cache paths must be declared"
    assert tle != gp, (
        "TLE_HISTORY_CACHE_DB and GP_HISTORY_CACHE_DB name the same file. "
        "They hold different schemas for different policy rules; sharing "
        "a file corrupts whichever module opens it second.")


def test_no_account_state_is_anchored_to_base_dir(assignments):
    # The specific regression: BASE_DIR is the module's own folder, so
    # two checkouts of the same code get two ledgers automatically.
    offenders = [n for n in ACCOUNT_STATE_CONSTANTS
                 if assignments.get(n) == "BASE_DIR"]
    assert not offenders, (
        f"anchored to BASE_DIR and therefore per-checkout: {offenders}")
