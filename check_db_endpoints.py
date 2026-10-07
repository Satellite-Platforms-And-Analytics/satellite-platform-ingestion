#!/usr/bin/env python
"""Which database endpoint actually completes a PostgreSQL handshake?

    python check_db_endpoints.py

Reads DATABASE_URL from the environment or .env, then tries the same
credentials against the transaction pooler (6543), the session pooler
(5432) and, if the username carries a project ref, the direct host.

NEVER PRINTS THE PASSWORD. It prints host, port, user and the exact
driver error, which is everything needed to tell these apart:

    "server didn't return client encoding"
        TCP connected, no PostgreSQL startup response. The peer is not
        speaking Postgres: a paused project, or a pooler that no longer
        knows this tenant.
    "password authentication failed"
        the endpoint is live and the credential is wrong.
    "timeout" / "could not connect"
        network or firewall, not the database.

WHY THE PORT MATTERS BEYOND REACHABILITY
========================================
6543 is the TRANSACTION pooler: a different backend per statement.
Session-scoped state does not survive it — `SET ROLE`, advisory locks,
session GUCs, some prepared-statement paths. Migrations and anything
that proves a privilege by BECOMING a role want 5432 or the direct
connection, not 6543.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse, urlunparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
except ImportError:
    sys.exit("psycopg2 is not installed in this environment")


def candidates(url: str):
    u = urlparse(url)
    host, port, user = u.hostname, u.port or 5432, u.username or ""

    yield ("transaction pooler", _swap(u, host, 6543))
    yield ("session pooler", _swap(u, host, 5432))

    # Supabase pooler usernames look like postgres.<project-ref>
    if "." in user and "pooler" in (host or ""):
        ref = user.split(".", 1)[1]
        yield ("direct", _swap(u, f"db.{ref}.supabase.co", 5432))


def _swap(u, host, port):
    netloc = u.netloc
    at = netloc.rfind("@")
    creds = netloc[:at + 1] if at >= 0 else ""
    return urlunparse(u._replace(netloc=f"{creds}{host}:{port}"))


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return print("DATABASE_URL is not set and was not found in .env") or 1

    any_ok = False
    for label, candidate in candidates(url):
        u = urlparse(candidate)
        print(f"\n{label:<20} {u.hostname}:{u.port}  user={u.username}")
        try:
            conn = psycopg2.connect(candidate, connect_timeout=10)
        except Exception as exc:                             # noqa: BLE001
            # The message can echo the DSN on some failures. Strip it.
            msg = str(exc).replace(u.password or "\0", "<redacted>").strip()
            print(f"  FAILED  {type(exc).__name__}: {msg}")
            continue
        any_ok = True
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_user, "
                        "current_setting('server_version_num')")
            ver, who, num = cur.fetchone()
        print(f"  OK      connected as {who}, server {num}")
        print(f"          {ver.split(' on ')[0]}")
        conn.close()

    if not any_ok:
        print("\nNo endpoint completed a handshake.")
        print("If every one says \"server didn't return client encoding\", "
              "the sockets are open and nothing is answering as Postgres — "
              "check whether the project is paused, and whether the pooler "
              "host in the dashboard still matches the one in .env.")
        return 1

    print("\nUse a 5432 endpoint for migrations and for any check that "
          "proves a privilege by becoming a role: 6543 hands out a "
          "different backend per statement, so SET ROLE does not persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
