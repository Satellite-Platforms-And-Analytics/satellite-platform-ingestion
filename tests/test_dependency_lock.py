"""
The lock files must actually cover requirements.txt, and the workflows
must actually use them.

Why this exists (AD-064)
------------------------
Until 2026-09-14 every scheduled workflow installed `-r requirements.txt`,
a list of `>=` floors, and so resolved fifteen packages fresh on each run.
Five of those workflows hold the database URL and the Space-Track
credentials. A compromised release of any dependency - direct or
transitive - would have been installed and executed with those secrets in
the environment, on a schedule, with nothing to notice it.

`requirements.lock` closes that: exact versions, every artifact's SHA-256,
and `pip install --require-hashes`, which refuses anything whose bytes do
not match.

The failure this test is really guarding against is not a bad lock. It is
a *bypassed* one - someone adds a package to requirements.txt and does not
regenerate, or adds a workflow that installs the .txt directly, and the
lock quietly stops describing what runs. Both are silent. Neither changes
any observable behaviour until the day it matters.

Offline and import-free, like test_requirements.py: this reads files as
text and never installs or imports anything it reasons about.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"

#: A pinned line in a uv/pip-compile lock: `name==version \`
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", re.M)

#: A requirement line in a hand-edited .txt: `name>=version`, `name`, ...
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:[<>=!~]|$)", re.M)


def _canon(name: str) -> str:
    """PEP 503 normalisation. `python-dotenv`, `python_dotenv` are one."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _declared(path: Path) -> set[str]:
    text = _strip_comments(path.read_text(encoding="utf-8"))
    return {_canon(m.group(1)) for m in _REQ.finditer(text)}


def _pinned(path: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for m in _PIN.finditer(_strip_comments(path.read_text(encoding="utf-8"))):
        out.setdefault(_canon(m.group(1)), set()).add(m.group(2))
    return out


LOCK_PAIRS = [
    ("requirements.txt", "requirements.lock"),
    ("requirements-dev.txt", "requirements-dev.lock"),
]


@pytest.mark.parametrize("txt,lock", LOCK_PAIRS)
def test_lock_exists(txt: str, lock: str) -> None:
    assert (REPO / lock).is_file(), (
        f"{lock} is missing. Regenerate it from {txt}:\n"
        f"  uv pip compile {txt} --universal --generate-hashes "
        f"--python-version 3.11 -o {lock}"
    )


@pytest.mark.parametrize("txt,lock", LOCK_PAIRS)
def test_every_declared_package_is_pinned(txt: str, lock: str) -> None:
    """
    The drift case. Adding a line to the .txt without regenerating leaves
    a package that is installed but not hash-checked - or, with
    --require-hashes, an install that fails in CI rather than here.
    """
    declared = _declared(REPO / txt)
    pinned = _pinned(REPO / lock)
    missing = sorted(declared - set(pinned))
    assert not missing, (
        f"{txt} declares {missing}, which {lock} does not pin. "
        f"Regenerate {lock}."
    )


@pytest.mark.parametrize("txt,lock", LOCK_PAIRS)
def test_every_pin_carries_a_hash(txt: str, lock: str) -> None:
    """
    A pin without hashes is a version, not a guarantee: it still trusts
    whatever bytes the index serves under that version.
    """
    text = (REPO / lock).read_text(encoding="utf-8")
    unhashed = []
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        m = _PIN.match(block)
        if m and "--hash=sha256:" not in block:
            unhashed.append(m.group(0))
    assert not unhashed, (
        f"{lock} pins {unhashed} with no hashes. It must be generated "
        f"with --generate-hashes."
    )


def test_no_workflow_installs_an_unpinned_requirements_file() -> None:
    """
    The bypass case, and the reason AD-064 was open for as long as it was:
    the lock can be perfect and still describe nothing, if the thing that
    runs on a schedule installs the floors instead.

    The audit job is exempt by design - pip-audit is *given* the floors on
    purpose, so that it reports on what the project permits rather than
    only on what it currently pins.
    """
    offenders = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for i, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if not s.startswith("pip install") or "pip-audit" in s:
                continue
            if "--upgrade pip" in s and "-r " not in s:
                continue
            if ".txt" in s:
                offenders.append(f"{wf.name}:{i}: {s}")
            elif ".lock" in s and "--require-hashes" not in s:
                offenders.append(f"{wf.name}:{i}: lock without --require-hashes")
    assert not offenders, (
        "these workflow steps install dependencies without hash "
        "verification:\n  " + "\n  ".join(offenders)
    )
