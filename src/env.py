"""
One place that loads .env for command-line entry points.

Modules read configuration from os.environ, which is correct: in GitHub
Actions the values arrive as repository secrets and no .env file exists.
But it means every entry point must load .env itself when run by hand,
and forgetting produces a confusing "DATABASE_URL is not set" on a
machine where DATABASE_URL is plainly sitting in .env.

Call load_env() at the top of main(). It is idempotent, and a no-op when
python-dotenv is not installed - which is the case in the slim CI installs,
where it is not needed anyway.
"""
from __future__ import annotations

_LOADED = False


def load_env() -> bool:
    """
    Load .env into os.environ if possible. Returns True if it was loaded.

    Existing environment variables win: python-dotenv does not override by
    default, so a value set by CI or the shell beats the file.
    """
    global _LOADED
    if _LOADED:
        return True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    load_dotenv()
    _LOADED = True
    return True
