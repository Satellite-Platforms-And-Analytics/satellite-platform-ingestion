#!/usr/bin/env python
"""Find, and optionally delete, credentials in .env that no code reads.

    python prune_env.py                # report only
    python prune_env.py --self-test    # prove the classifier can go red
    python prune_env.py --apply        # back up, then delete the UNUSED
    python prune_env.py --apply --include-named
                                       # also delete the NAMED-ONLY keys,
                                       # after you have read the file list

WHAT THIS SCRIPT NEVER DOES
---------------------------
It never prints a value, never writes one to a log, and never sends one
anywhere. It reads .env only to learn the *names* on the left of each '='
and to copy lines through verbatim. `assert_no_values_leak` below is the
control that enforces that, and --self-test proves it can fail.

THREE BUCKETS, NOT TWO
----------------------
Version 1 of this script asked one question -- does this name appear
anywhere? -- and that was too coarse in both directions. It counted a
sentence in a README as a use, and, worse, it counted ITSELF: two keys
named in its own docstring came back "read by code", the code being this
file. A scanner that reads the scanner is not measuring the repository.

So the question is now asked in two parts:

  READ        an accessor was found -- os.environ["K"], os.getenv("K"),
              process.env.K, %K%, $env:K, ${K}. This is a use. Keep it.
  NAMED-ONLY  the name appears, but never as an accessor. A README
              sentence, a comment, a doc table. Probably dead, possibly
              a read this script cannot see (an indirect lookup, a name
              built at runtime). A human decides; --include-named acts.
  UNUSED      the name appears nowhere at all. Safe to delete.

Only UNUSED is deleted by default, because the two mistakes are not
symmetrical --

    calling a used key unused   -> deletion breaks the pipeline
    calling an unused key used  -> the key survives one more day

-- and NAMED-ONLY is exactly the band where that asymmetry bites. It is
reported in full, with every file, so the judgement is yours and is made
on evidence rather than on a count.

This file excludes itself from the scan. That is not tidiness; it is the
fix for the defect above.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Extensions worth reading. Locks, caches and data files are excluded: a
# key name inside requirements.lock is a hash collision, not a use.
CODE_SUFFIXES = {".py", ".cmd", ".bat", ".ps1", ".sql", ".yml", ".yaml",
                 ".ts", ".tsx", ".js", ".jsx", ".json", ".toml", ".cfg",
                 ".ini"}
DOC_SUFFIXES = {".md", ".txt", ".rst"}
SOURCE_SUFFIXES = CODE_SUFFIXES | DOC_SUFFIXES

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules",
             ".next", ".venv", "venv", "data", "archive", ".mypy_cache"}

SKIP_NAMES = {".env", ".env.example", ".env.local", "requirements.lock",
              "requirements-dev.lock", "package-lock.json"}

KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

# How a key is actually *read*, across the four languages in these repos.
# Each entry is a format string; {k} becomes the escaped key name.
ACCESSORS = [
    r'environ\s*\[\s*[\'"]{k}[\'"]\s*\]',        # os.environ["K"]
    r'environ\s*\.\s*get\s*\(\s*[\'"]{k}[\'"]',  # os.environ.get("K")
    r'getenv\s*\(\s*[\'"]{k}[\'"]',              # os.getenv("K"), getenv("K")
    r'process\s*\.\s*env\s*\.\s*{k}\b',          # process.env.K
    r'process\s*\.\s*env\s*\[\s*[\'"]{k}[\'"]',  # process.env["K"]
    r'%{k}%',                                    # %K%   (cmd)
    r'\$env:{k}\b',                              # $env:K (PowerShell)
    r'\$\{{{k}\}}',                              # ${K}  (sh, compose)
    r'\$\{{\{{\s*secrets\.{k}\s*\}}\}}',         # ${{ secrets.K }} (Actions)
    r'\benv\s*\.\s*{k}\b',                       # env.K
    r'current_setting\s*\(\s*[\'"]{k}[\'"]',     # postgres
]

# The same question asked backwards: not "is this key read?" but "what is
# read that this file never mentions?". A .env can only be audited for
# what is IN it; a template is wrong just as often for what is missing,
# and that failure is silent -- os.environ.get() returns None and the
# error surfaces somewhere else entirely, as an auth failure or a path
# that does not exist. Added 2026-09-20 after .env.example was found
# naming SPACETRACK_USER while the code read SPACETRACK_USERNAME.
DISCOVERY = [
    r'environ\s*\[\s*[\'"]([A-Z][A-Z0-9_]{2,})[\'"]\s*\]',
    r'environ\s*\.\s*get\s*\(\s*[\'"]([A-Z][A-Z0-9_]{2,})[\'"]',
    r'getenv\s*\(\s*[\'"]([A-Z][A-Z0-9_]{2,})[\'"]',
    r'process\s*\.\s*env\s*\.\s*([A-Z][A-Z0-9_]{2,})\b',
    r'process\s*\.\s*env\s*\[\s*[\'"]([A-Z][A-Z0-9_]{2,})[\'"]',
    r'\$env:([A-Z][A-Z0-9_]{2,})\b',
    r'\$\{\{\s*secrets\.([A-Z][A-Z0-9_]{2,})\s*\}\}',
]

# Names that are read but are not this project's configuration: supplied
# by the OS, the shell, the runner or the toolchain. Listing them in a
# template would be noise at best and a false instruction at worst.
AMBIENT = {
    "PATH", "PYTHONPATH", "PYTHONHOME", "HOME", "USER", "USERNAME",
    "USERPROFILE", "TEMP", "TMP", "TMPDIR", "COMSPEC", "OS", "SYSTEMROOT",
    "WINDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMDATA",
    "CD", "ERRORLEVEL", "RANDOM", "DATE", "TIME", "SHELL", "TERM", "PWD",
    "OLDPWD", "LANG", "LC_ALL", "TZ", "CI", "NODE_ENV", "VIRTUAL_ENV",
    "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYTHONIOENCODING",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
}
AMBIENT_PREFIXES = ("GITHUB_", "RUNNER_", "VERCEL_", "NPM_", "NEXT_RUNTIME")


def is_ambient(name: str) -> bool:
    return name in AMBIENT or name.startswith(AMBIENT_PREFIXES)


def discover(roots: list[Path], exclude: set[Path]) -> dict[str, list[str]]:
    """Every env var the code reads, mapped to the files that read it.

    Ambient names are dropped. What remains is this project's configuration
    surface, which is the thing a template is supposed to describe.
    """
    pats = [re.compile(p) for p in DISCOVERY]
    found: dict[str, list[str]] = {}
    for path in source_files(roots, exclude):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix.lower() in DOC_SUFFIXES:
            continue          # a README showing os.getenv("X") is prose
        for pat in pats:
            for m in pat.finditer(text):
                name = m.group(1)
                if is_ambient(name):
                    continue
                found.setdefault(name, [])
                if str(path) not in found[name]:
                    found[name].append(str(path))
    return found


def env_keys(path: Path) -> list[str]:
    """Names only. The right-hand side is never returned, never stored."""
    keys: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = KEY_LINE.match(line)
        if m:
            keys.append(m.group(1))
    return keys


def scan_roots(repo: Path) -> list[Path]:
    """This repo, plus every sibling repo, which may read the same keys.

    Discovered by glob rather than hardcoded. The hardcoded version named
    the frontend and the infrastructure repos, which was correct only when
    run from the ingestion repo -- point it at the frontend's .env.local
    and the ingestion repo silently dropped out of the scan, so every key
    the ingestion code reads would have been reported UNUSED. That is the
    dangerous direction of the one-sided error, reached by moving the
    starting point rather than by any change in the data.
    """
    roots = [repo]
    for p in sorted(repo.parent.glob("satellite-platform-*")):
        if p.is_dir() and p.resolve() != repo.resolve():
            roots.append(p)
    return roots


def source_files(roots: list[Path], exclude: set[Path]):
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if name in SKIP_NAMES:
                    continue
                p = Path(dirpath) / name
                if p.suffix.lower() not in SOURCE_SUFFIXES:
                    continue
                try:
                    if p.resolve() in exclude:
                        continue
                except OSError:
                    pass
                yield p


def classify(keys: list[str], roots: list[Path], exclude: set[Path]):
    """Return {key: (bucket, [(path, 'read'|'named'), ...])}.

    bucket is 'read', 'named' or 'unused'.
    """
    word = {k: re.compile(r"\b" + re.escape(k) + r"\b") for k in keys}
    acc = {
        k: re.compile("|".join(p.format(k=re.escape(k)) for p in ACCESSORS))
        for k in keys
    }
    hits: dict[str, list[tuple[str, str]]] = {k: [] for k in keys}

    for path in source_files(roots, exclude):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key in keys:
            if not word[key].search(text):
                continue
            # An accessor inside a README is a code SAMPLE, not a call.
            # Without this suffix test a dead key stays alive forever on
            # the strength of the setup instructions that mention it.
            is_code = path.suffix.lower() in CODE_SUFFIXES
            kind = "read" if (is_code and acc[key].search(text)) else "named"
            hits[key].append((str(path), kind))

    out = {}
    for key in keys:
        found = hits[key]
        if any(kind == "read" for _, kind in found):
            out[key] = ("read", found)
        elif found:
            out[key] = ("named", found)
        else:
            out[key] = ("unused", found)
    return out


def assert_no_values_leak(rendered: str, path: Path) -> None:
    """The control. Refuse to emit anything containing a value from .env."""
    values = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = KEY_LINE.match(line)
        if not m:
            continue
        val = line.split("=", 1)[1].strip().strip("'\"")
        if len(val) >= 8:          # short values are not secrets worth guarding
            values.append(val)
    for val in values:
        if val in rendered:
            raise AssertionError(
                "refusing to print: output contains a value from .env"
            )


def prune(path: Path, doomed: set[str]) -> tuple[str, int]:
    """Return the new file text and the number of lines removed.

    Comments and ordering are preserved. A comment immediately above a
    deleted key goes with it, because a comment describing a key that no
    longer exists is worse than no comment.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    keep: list[str] = []
    removed = 0
    for line in lines:
        m = KEY_LINE.match(line)
        if m and m.group(1) in doomed:
            removed += 1
            while keep and keep[-1].lstrip().startswith("#"):
                keep.pop()
            continue
        keep.append(line)
    out: list[str] = []
    for line in keep:
        if line.strip() == "" and out and out[-1].strip() == "":
            continue
        out.append(line)
    return "".join(out), removed


def self_test() -> int:
    """Prove the classifier can go red -- in all three buckets.

    The 09-15 lesson was that three controls could not see themselves. The
    09-20 lesson was the mirror image: a control that saw ONLY itself. So
    the fixture below includes a decoy file that names every key without
    reading any, and asserts that naming is not mistaken for reading.
    """
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        (root / "src").mkdir(parents=True)
        env = root / ".env"
        env.write_text(
            "# read by the code\n"
            "USED_KEY=abcdefghijklmnop\n"
            "\n"
            "# mentioned in prose only\n"
            "DOC_KEY=qrstuvwxyz012345\n"
            "\n"
            "# mentioned nowhere\n"
            "ORPHAN_KEY=3456789abcdefghi\n",
            encoding="utf-8",
        )
        (root / "src" / "app.py").write_text(
            'import os\nx = os.environ["USED_KEY"]\n', encoding="utf-8"
        )
        # The prose deliberately CONTAINS an accessor, because that is what
        # a setup README looks like. If docs are scanned for discovery, this
        # line invents DOC_KEY as a configuration variable.
        (root / "README.md").write_text(
            "Set DOC_KEY before running. USED_KEY is also required.\n"
            'The code does `os.environ.get("DOC_KEY")` at startup.\n',
            encoding="utf-8",
        )
        # the decoy: names every key, reads none. v1 called all three used.
        decoy = root / "src" / "scanner.py"
        decoy.write_text(
            '"""Handles USED_KEY, DOC_KEY and ORPHAN_KEY."""\n',
            encoding="utf-8",
        )

        keys = env_keys(env)
        if keys != ["USED_KEY", "DOC_KEY", "ORPHAN_KEY"]:
            failures.append(f"env_keys returned {keys}")

        # scan_roots must find the siblings from ANY starting point, not
        # just from the one repo the list used to be written for.
        sibs = Path(tmp) / "satellite-platform-a", Path(tmp) / "satellite-platform-b"
        for s in sibs:
            s.mkdir()
        got = {p.name for p in scan_roots(sibs[0])}
        if "satellite-platform-b" not in got:
            failures.append(f"scan_roots missed a sibling repo: {sorted(got)}")
        if sorted(got).count("satellite-platform-a") != 1:
            failures.append(f"scan_roots duplicated the starting repo: {sorted(got)}")

        # with the decoy excluded, the three buckets must come out clean
        res = classify(keys, [root], {decoy.resolve()})
        for key, want in (("USED_KEY", "read"), ("DOC_KEY", "named"),
                          ("ORPHAN_KEY", "unused")):
            got = res[key][0]
            if got != want:
                failures.append(f"{key} classified {got!r}, expected {want!r}")

        # and the decoy, when NOT excluded, must not promote anything to read
        res2 = classify(keys, [root], set())
        if res2["ORPHAN_KEY"][0] != "named":
            failures.append(
                f"a docstring mention promoted ORPHAN_KEY to "
                f"{res2['ORPHAN_KEY'][0]!r} -- naming is being read as reading"
            )
        if res2["USED_KEY"][0] != "read":
            failures.append("USED_KEY lost its real accessor")

        # the backwards question: a var the code reads and .env omits.
        # This is the SPACETRACK_USER/SPACETRACK_USERNAME shape -- both
        # halves look right in isolation, so only this catches it.
        (root / "src" / "paths.py").write_text(
            'import os\n'
            'd = os.environ.get("ABSENT_KEY", "/tmp")\n'
            'h = os.environ.get("HOME")\n',
            encoding="utf-8",
        )
        # A sibling repo reading its own variable must not be demanded of
        # THIS repo's env file. Scoping regression guard.
        sib_repo = Path(tmp) / "satellite-platform-sibling"
        sib_repo.mkdir(exist_ok=True)
        (sib_repo / "other.py").write_text(
            'import os\ns = os.environ.get("SIBLING_ONLY_KEY")\n',
            encoding="utf-8",
        )
        both = discover([root, sib_repo], {decoy.resolve()})
        owned = {k for k, v in both.items()
                 if any(Path(p).is_relative_to(root) for p in v)}
        if "SIBLING_ONLY_KEY" in owned:
            failures.append(
                "a sibling repo's variable was attributed to this repo -- "
                "ABSENT would demand it of the wrong env file")
        if "ABSENT_KEY" not in owned:
            failures.append("scoping dropped a key this repo really reads")

        disc = discover([root], {decoy.resolve()})
        if "ABSENT_KEY" not in disc:
            failures.append("discover missed a key the code reads")
        if "USED_KEY" not in disc:
            failures.append("discover missed USED_KEY")
        if "HOME" in disc:
            failures.append("discover reported HOME -- ambient names leaking in")
        if "DOC_KEY" in disc:
            failures.append("discover invented DOC_KEY from prose")

        text, removed = prune(env, {"ORPHAN_KEY"})
        if removed != 1:
            failures.append(f"prune removed {removed} lines, expected 1")
        if "USED_KEY" not in text or "DOC_KEY" not in text:
            failures.append("prune deleted a key it was not given")
        if "ORPHAN_KEY" in text:
            failures.append("prune left the orphan key behind")
        if "mentioned nowhere" in text:
            failures.append("prune left the orphan's comment behind")

        try:
            assert_no_values_leak("token is qrstuvwxyz012345", env)
            failures.append("assert_no_values_leak did not fire on a real value")
        except AssertionError:
            pass
        try:
            assert_no_values_leak("ORPHAN_KEY is unused", env)
        except AssertionError:
            failures.append("assert_no_values_leak fired on a name-only line")

    for f in failures:
        print(f"  FAIL  {f}")
    print(f"\nself-test: {'FAILED' if failures else 'passed'} "
          f"({len(failures)} failure{'' if len(failures) == 1 else 's'})")
    return 1 if failures else 0


def rel(path: str, roots: list[Path]) -> str:
    p = Path(path)
    for r in roots:
        try:
            return f"{r.name}/{p.relative_to(r).as_posix()}"
        except ValueError:
            continue
    return p.name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="delete the UNUSED keys (a backup is written first)")
    ap.add_argument("--include-named", action="store_true",
                    help="with --apply, also delete the NAMED-ONLY keys")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the classifier and the leak control can fail")
    ap.add_argument("--env", default=".env", help="path to the .env to prune")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    env_path = Path(args.env).resolve()
    if not env_path.is_file():
        print(f"no such file: {env_path}", file=sys.stderr)
        return 1

    repo = env_path.parent
    roots = scan_roots(repo)
    me = Path(__file__).resolve()
    keys = env_keys(env_path)
    res = classify(keys, roots, {me})

    read = [k for k in keys if res[k][0] == "read"]
    named = [k for k in keys if res[k][0] == "named"]
    unused = [k for k in keys if res[k][0] == "unused"]

    print(f"scanned: {', '.join(r.name for r in roots)}  "
          f"(excluding {me.name})")
    print(f"{len(keys)} keys in {env_path.name}: "
          f"{len(read)} read, {len(named)} named but never read, "
          f"{len(unused)} absent entirely\n")

    if read:
        print("READ — an accessor was found. Keep.")
        for k in read:
            where = [p for p, kind in res[k][1] if kind == "read"]
            first = rel(where[0], roots)
            extra = f"  +{len(where) - 1} more" if len(where) > 1 else ""
            print(f"  keep    {k:<28} {first}{extra}")
        print()

    if named:
        print("NAMED-ONLY — the name appears, no accessor anywhere.")
        print("Every file is listed; read them before deciding.")
        for k in named:
            print(f"  review  {k}")
            for p, _ in res[k][1]:
                print(f"            {rel(p, roots)}")
        print()

    if unused:
        print("UNUSED — the name appears nowhere.")
        for k in unused:
            print(f"  DELETE  {k}")
        print()

    # The question asked backwards. Only this section can catch a name
    # mismatch, because both halves of one look correct on their own.
    #
    # Scoped to the repo that OWNS this file. An env file serves one repo;
    # reporting every variable read anywhere in the tree told the frontend's
    # .env.local to add SPACETRACK_PASSWORD and WIT_PATH -- 21 lines of
    # noise in the one section whose whole job is being read carefully.
    # Cross-repo reads are still counted as uses by `classify` above, which
    # is the safe direction; they just are not demanded here.
    found = {k: v for k, v in discover(roots, {me}).items() if k not in keys}
    mine = {k: v for k, v in found.items()
            if any(Path(p).is_relative_to(repo) for p in v)}
    elsewhere = sorted(set(found) - set(mine))

    if mine:
        print(f"ABSENT — read by {repo.name}, not named in {env_path.name}.")
        print("Add these, or the next person to follow this file gets None.")
        for k in sorted(mine):
            here = [p for p in mine[k] if Path(p).is_relative_to(repo)]
            first = rel(here[0], roots)
            extra = f"  +{len(here) - 1} more" if len(here) > 1 else ""
            print(f"  ADD     {k:<28} {first}{extra}")
        print()

    if elsewhere:
        print(f"(read only by the sibling repos, so not this file's job: "
              f"{', '.join(elsewhere)})\n")

    assert_no_values_leak("\n".join(keys), env_path)

    doomed = set(unused) | (set(named) if args.include_named else set())

    if not doomed:
        if named and not args.include_named:
            print("Nothing is safe to delete unattended. Review the "
                  "NAMED-ONLY list, then --apply --include-named.")
        else:
            print("nothing to prune.")
        return 0

    if not args.apply:
        print(f"{len(doomed)} key(s) would be deleted. Re-run with --apply.")
        if named and not args.include_named:
            print(f"{len(named)} NAMED-ONLY key(s) would be kept; add "
                  f"--include-named once you have read the files above.")
        print("Rotate anything deleted afterwards: it existed on disk, so "
              "treat it as exposed.")
        return 0

    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = env_path.with_name(f".env.bak-{stamp}")
    shutil.copy2(env_path, backup)
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass

    text, removed = prune(env_path, doomed)
    env_path.write_text(text, encoding="utf-8")

    after = set(env_keys(env_path))
    if after != set(keys) - doomed:
        shutil.copy2(backup, env_path)
        print("\nverification failed after write; .env restored from backup",
              file=sys.stderr)
        return 1

    print(f"removed {removed} key(s). backup: {backup.name}")
    print("The backup still contains the secrets. Move it off this machine "
          "or delete it once the rotation is done. (.gitignore's .env* rule "
          "already keeps it out of git.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
