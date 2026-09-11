"""
Every third-party import in this repository must be declared in one of the
requirements files, and every declared package must actually be imported.

Why this exists
---------------
On 2026-09-10 an audit found requirements.txt wrong in both directions at
once. Ten packages were declared that nothing imported - aiohttp,
geopandas, shapely, pyproj, fiona, dask, xarray, alembic, apscheduler,
pytest-asyncio - and four packages that src/tracking/ imports at module
scope were declared nowhere at all: pandas, openpyxl, tqdm, psutil (and
spacetrack). The tool worked anyway because the workstation happened to
have them installed.

The declared-but-unused half was not merely untidy. Four of those ten
were named in five separate workflow headers as the reason those
workflows could not use `pip install -r requirements.txt`, so a list of
dependencies that did not exist was the stated justification for six
hand-maintained install lists that then drifted from each other. CI
caught one of those drifts on 2026-09-06 only because a test happened to
import a module whose file had not been staged.

This test is deliberately offline and import-free: it reads source with
ast and requirements files as text. It never imports the packages it is
reasoning about, so it passes in a bare environment.
"""
from __future__ import annotations

import ast
import re
import sys
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

def _requirements_files() -> list[str]:
    """
    Every requirements*.txt in the repo root, discovered rather than
    listed.

    A hardcoded list has to be edited whenever a group is added, and the
    failure when someone forgets is this test reporting a package as
    undeclared when it is declared - a false accusation that costs more
    time than the file it was guarding. That happened on 2026-09-11 when
    requirements-archive.txt was added for pyarrow.

    requirements.txt is asserted separately: the core file is the one the
    scheduled workflows install, so its absence is a different failure
    from a missing optional group.
    """
    found = sorted(p.name for p in REPO.glob("requirements*.txt"))
    assert "requirements.txt" in found, (
        "requirements.txt is missing - every scheduled workflow installs it")
    return found


REQUIREMENTS_FILES = _requirements_files()

# Distribution name -> the module name it provides, where the two differ.
# Only packages this repository actually declares need an entry.
DIST_TO_MODULE = {
    "psycopg2-binary": "psycopg2",
    "python-dotenv": "dotenv",
}

# Declared on purpose without ever being imported. Anything not on this
# list has to be imported by something or the test fails.
#
# ruff and mypy are command-line tools; a test that demanded `import ruff`
# would be testing the wrong thing. pytest is imported by the test files
# themselves, so it is not exempt and does not need to be.
TOOLS_NOT_IMPORTED = {"ruff", "mypy"}

# Local modules that are importable by bare name because src/tracking/
# inserts its own directory on sys.path. They are first-party files in
# this repository, not distributions, and must not be mistaken for
# undeclared dependencies. Verified present as .py files below, so this
# list cannot silently hide a real missing package.
LOCAL_FLAT_MODULES = {
    "api_request_log", "catalog_diff", "config", "historical_accuracy",
    "log_utils", "report_utils", "reports", "run_state", "satcat_cache",
    "satellite_confidence_db", "satellite_utils", "sensor_select",
    "spacetrack_client", "spacetrack_policy_check", "tle_bulk_seeder",
    "tle_history_cache", "writer",
}

# Modules that are neither first-party nor installable: they resolve only
# because a module mutates sys.path at import time to point at another
# project on the same workstation.
#
# `wit` is src/resources.py inserting D:\Projects\WIT. It is the only
# cross-project dependency in this repository and its future is an open
# decision (CODE_AUDIT.md). It is exempted here by name, with this note,
# rather than buried in LOCAL_FLAT_MODULES where it would look like an
# ordinary local file - a dependency on another checkout that no
# requirements file can express should be visible in the test that
# governs dependencies. test_external_sys_path_modules_have_not_grown
# fails if a second one appears.
EXTERNAL_SYS_PATH_MODULES = {"wit"}

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache",
             "build", "dist", ".mypy_cache", ".ruff_cache",
             # ci.yml checks the sibling infrastructure repository out
             # here for the schema drift guard. actions/checkout's
             # sparse-checkout is cone mode by default, which brings
             # top-level files along with schema/ - including
             # apply_migration.py. That is another repository's code and
             # another repository's dependencies; scanning it would make
             # this repository's CI fail for a change made elsewhere.
             "_infrastructure"}


@lru_cache(maxsize=1)
def _python_files() -> tuple[Path, ...]:
    out = []
    for p in REPO.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return tuple(sorted(out))


def _top_level_imports(path: Path) -> set[str]:
    # utf-8-sig, not utf-8: two check scripts in this repository carry a
    # BOM. CPython's own loader strips it, but ast.parse on a plain utf-8
    # read does not, and the file fails with "invalid non-printable
    # character U+FEFF". A scanner that skips those files silently is
    # worse than no scanner.
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import: first-party by definition.
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


@lru_cache(maxsize=1)
def _declared() -> dict[str, str]:
    """distribution name (lowercased) -> the file that declares it."""
    found: dict[str, str] = {}
    for name in REQUIREMENTS_FILES:
        path = REPO / name
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            dist = re.split(r"[<>=!~\[;]", line, 1)[0].strip().lower()
            if dist:
                found[dist] = name
    return found


@lru_cache(maxsize=1)
def _third_party_imports() -> dict[str, tuple[str, ...]]:
    """module name -> set of relative paths importing it."""
    stdlib = set(sys.stdlib_module_names)
    first_party_pkgs = {"src", "tests"}
    acc: dict[str, set[str]] = {}
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        for mod in _top_level_imports(path):
            if mod in stdlib or mod.startswith("_"):
                continue
            if mod in first_party_pkgs or mod in LOCAL_FLAT_MODULES:
                continue
            if mod in EXTERNAL_SYS_PATH_MODULES:
                continue
            # A root-level script imported by a test, e.g. check_new_objects.
            if (REPO / f"{mod}.py").exists():
                continue
            acc.setdefault(mod, set()).add(rel)
    return {mod: tuple(sorted(files)) for mod, files in acc.items()}


def test_every_third_party_import_is_declared():
    declared = _declared()
    modules = {DIST_TO_MODULE.get(d, d).lower() for d in declared}
    undeclared = {
        mod: list(files)
        for mod, files in _third_party_imports().items()
        if mod.lower() not in modules
    }
    assert not undeclared, (
        "imported but not declared in any requirements file:\n"
        + "\n".join(f"  {m}: {', '.join(f)}" for m, f in sorted(undeclared.items()))
        + "\nAdd it to the requirements file for the package that needs it, "
          "or to LOCAL_FLAT_MODULES if it is a first-party module."
    )


def test_every_declared_package_is_imported():
    imported = {m.lower() for m in _third_party_imports()}
    unused = sorted(
        f"{dist} (declared in {where})"
        for dist, where in _declared().items()
        if dist not in TOOLS_NOT_IMPORTED
        and DIST_TO_MODULE.get(dist, dist).lower() not in imported
    )
    assert not unused, (
        "declared but imported by nothing:\n  " + "\n  ".join(unused)
        + "\nRemove it, or add it to TOOLS_NOT_IMPORTED with a reason if it "
          "is a command-line tool rather than a library."
    )


def test_core_requirements_cover_the_scheduled_workflows():
    """
    The five scheduled workflows install `-r requirements.txt` and nothing
    else. If a module reachable from src/ but outside src/tracking/ and
    src/imagery/ needs a package from one of the optional files, those
    jobs would fail at import time on the runner and nowhere else.
    """
    core = {
        DIST_TO_MODULE.get(d, d).lower()
        for d, where in _declared().items()
        if where == "requirements.txt"
    }
    optional_only = []
    for mod, files in _third_party_imports().items():
        scheduled = [
            f for f in files
            if f.startswith("src/")
            and not f.startswith("src/tracking/")
            and not f.startswith("src/imagery/")
        ]
        if scheduled and mod.lower() not in core:
            optional_only.append(f"{mod}: {', '.join(sorted(scheduled))}")
    assert not optional_only, (
        "reachable from a scheduled workflow but not in requirements.txt:\n  "
        + "\n  ".join(sorted(optional_only))
    )


def test_external_sys_path_modules_have_not_grown():
    """
    Reaching into another project on the same disk is a coupling, not a
    dependency, and it cannot be installed, pinned or audited. One such
    module is a known open decision; a second one appearing without a
    deliberate choice is what this guards against.
    """
    assert EXTERNAL_SYS_PATH_MODULES == {"wit"}, (
        "a new cross-project sys.path dependency appeared: "
        + ", ".join(sorted(EXTERNAL_SYS_PATH_MODULES - {"wit"}))
        + ". These cannot be declared in a requirements file. Decide "
          "deliberately before adding one."
    )


def test_local_flat_modules_are_real_files():
    """
    LOCAL_FLAT_MODULES suppresses names from the undeclared check, so an
    entry that no longer corresponds to a file would hide a genuinely
    missing dependency. Every name must still resolve to a .py file.
    """
    stems = {p.stem for p in _python_files()}
    missing = sorted(LOCAL_FLAT_MODULES - stems)
    assert not missing, (
        "LOCAL_FLAT_MODULES names no longer backed by a file: "
        + ", ".join(missing)
        + ". Remove them - each one is currently suppressing a real check."
    )


@pytest.mark.parametrize("name", REQUIREMENTS_FILES)
def test_requirements_file_parses_and_is_not_empty(name):
    declared = [d for d, where in _declared().items() if where == name]
    assert declared, f"{name} declares nothing"
