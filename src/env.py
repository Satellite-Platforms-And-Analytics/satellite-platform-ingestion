"""
One place that loads .env for command-line entry points.

Modules read configuration from os.environ, which is correct: in GitHub
Actions the values arrive as repository secrets and no .env file exists.
But it means every entry point must load .env itself when run by hand,
and forgetting produces a confusing "DATABASE_URL is not set" on a
machine where DATABASE_URL is plainly sitting in .env.

Call `bootstrap()` at the top of main(). It is idempotent.

WHY BOOTSTRAP AND NOT JUST load_env
===================================
Every entry point used to do:

    try:
        from src.env import load_env
        load_env()
    except ImportError:
        pass

which fails silently in the one case that matters. `python -m src.tle.fetcher`
puts the repo root on sys.path and works. `python src/tle/fetcher.py` puts
`src/tle` there instead, so `import src.env` raises ImportError, the
`pass` swallows it, .env is never read, and the run dies several steps
later claiming DATABASE_URL is not set - on a machine where it is set.

Observed 2026-09-04. It is the project's recurring shape once more: a
fallback that hides the failure it was meant to tolerate.

`bootstrap()` puts the repo root on sys.path first, so the import cannot
fail for that reason, and reports honestly when python-dotenv is genuinely
absent rather than pretending nothing happened.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Repo root - this file is <root>/src/env.py.
REPO_ROOT = Path(__file__).resolve().parents[1]

_LOADED = False


def bootstrap() -> bool:
    """
    Make `import src.*` work regardless of how this process was started,
    then load .env. Returns True if .env was read.

    Safe to call from any entry point, as a script or as a module.
    """
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return load_env()


def load_env() -> bool:
    """
    Load .env into os.environ if possible. Returns True if it was loaded.

    The file is located relative to this module rather than the working
    directory, so it is found whether the command was run from the repo
    root, from src/, or from anywhere else.

    Existing environment variables win: python-dotenv does not override by
    default, so a value set by CI or the shell beats the file.
    """
    global _LOADED
    if _LOADED:
        return True

    try:
        from dotenv import load_dotenv
    except ImportError:
        # Genuinely absent - the slim CI installs skip it, and there the
        # values come from repository secrets. Say so at debug level
        # rather than silently continuing.
        log.debug("python-dotenv is not installed; using the environment "
                  "as-is. This is expected in CI.")
        return False

    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        log.debug("Loaded %s", env_path)
    else:
        load_dotenv()          # nothing at the root; let dotenv look around
    _LOADED = True
    return True
