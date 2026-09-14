#!/usr/bin/env python
"""
Isolate the TechPort 403: is it the key, the client, or the network?

The finding that prompted this
------------------------------
On 2026-09-14 `check_techport.py` came back 403 with an **Apache HTML**
body - "You don't have permission to access this resource." That is not
api.data.gov's error. api.data.gov refuses a key with a JSON body naming
the fault (API_KEY_INVALID and friends). An HTML 403 means the request
was refused by something *in front of* the key check, so the key was
never examined and is not implicated.

Two facts were established independently before this script was written:

  - the exact URL serves 1,000+ projects to a browser-like client, right
    now, on DEMO_KEY. So the endpoint is healthy and the path is correct
  - the key in .env loads at 40 characters, alphanumeric, nothing to
    strip. So it is well formed

What differs between the working request and the failing one is the
*client*, and two things vary at once: the User-Agent, and the source
network. This script separates them.

How it reads
------------
DEMO_KEY only. Your key is never sent and never read, so nothing here can
spend its quota or leak it.

DEMO_KEY's published limits are **30 requests/hour AND 50 requests/day,
both per IP address** (api.data.gov developer manual, read 2026-09-14).
This spends 4. The daily cap is the one worth remembering: it is not
mentioned in most examples, it is shared with anything else on this IP
using DEMO_KEY, and twelve runs of this script would reach it. If that
happens the answer is to wait, not to switch to the real key - the whole
design of this diagnostic is that the real key stays out of it.

The key is passed as a query parameter here, deliberately, even though
check_techport.py now uses the X-Api-Key header: this script's job is to
reproduce the request that failed on 2026-09-14, and changing two
variables at once is how a reproduction stops being one. DEMO_KEY is
public, so there is nothing to leak.

  all four 403 (HTML)   -> the source network, not the client. A VPN,
                           a corporate egress, or an IP range the WAF
                           refuses. Try a different network
  default UA 403,
  descriptive UA 200    -> the WAF rejects anonymous programmatic
                           clients. Fix: send a descriptive User-Agent.
                           This is what well-behaved API clients are
                           asked to do anyway
  only browser UA 200   -> READ THE NOTE BELOW BEFORE CHANGING ANYTHING
  all four 200          -> it is transient, or it is the key after all.
                           Re-run check_techport.py

On the browser User-Agent
-------------------------
It is included here as a *measurement*, because "does a browser UA pass?"
is the finding that distinguishes the two cases above. It is deliberately
not a fix.

A descriptive User-Agent that names the project and a contact is honest
identification, and most data providers explicitly ask for one. A
User-Agent that claims to be Chrome is a claim that a person is browsing.
If that is the only thing that works, the operator is telling us it does
not want programmatic traffic on this route, and the answer is to stop
and reconsider the source - not to dress the client up. That is the same
call AD-060 already made about the unmetered techport.nasa.gov host.
"""
from __future__ import annotations

import os
import sys

from src.env import bootstrap

bootstrap()

import requests                                              # noqa: E402

URL = "https://api.nasa.gov/techport/api/projects"
PARAMS = {"updatedSince": "2020-01-01", "api_key": "DEMO_KEY"}

CONTACT = os.environ.get("CONTACT_EMAIL", "").strip()
DESCRIPTIVE = (
    "satellite-platform-ingestion/1.0 (research; "
    + (f"contact {CONTACT}" if CONTACT else "non-commercial")
    + ")"
)
BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

CASES = [
    ("requests default", None),
    ("descriptive", DESCRIPTIVE),
    ("browser", BROWSER),
    ("descriptive + browser Accept", DESCRIPTIVE),
]


def body_kind(r: requests.Response) -> str:
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        try:
            b = r.json()
        except Exception:                                    # noqa: BLE001
            return "json (unparseable)"
        if isinstance(b, dict) and "projects" in b:
            return f"json, {len(b['projects'])} projects"
        if isinstance(b, dict) and "error" in b:
            err = b["error"]
            code = err.get("code") if isinstance(err, dict) else err
            return f"json error: {code}"
        return "json"
    if "html" in ctype:
        return "HTML (a layer in front of api.data.gov)"
    return ctype or "unknown"


def main() -> int:
    print("TechPort 403 isolation - DEMO_KEY only, your key is not read\n")
    if not CONTACT:
        print("  note: set CONTACT_EMAIL in .env to put a real contact in")
        print("        the descriptive User-Agent. Running without one.\n")

    results = []
    for i, (label, ua) in enumerate(CASES):
        headers = {}
        if ua:
            headers["User-Agent"] = ua
        if label.endswith("browser Accept"):
            headers["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "*/*;q=0.8")
            headers["Accept-Language"] = "en-US,en;q=0.9"
        try:
            r = requests.get(URL, params=PARAMS, headers=headers, timeout=30)
        except requests.RequestException as exc:
            print(f"  {label:30s} -> request failed: {type(exc).__name__}")
            results.append((label, None))
            continue
        lim = r.headers.get("X-RateLimit-Limit")
        rem = r.headers.get("X-RateLimit-Remaining")
        quota = f"  [{rem}/{lim} left]" if (lim and rem) else ""
        print(f"  {label:30s} -> {r.status_code}  {body_kind(r)}{quota}")
        results.append((label, r.status_code))
        if rem and rem.isdigit() and int(rem) < 10:
            print(f"       ^ DEMO_KEY is nearly spent on this IP "
                  f"({rem} left). Wait an hour rather than reaching for")
            print(f"         the real key - keeping it out of this "
                  f"diagnostic is the point of the diagnostic.")

    ok = {label for label, code in results if code == 200}
    print()
    if not ok:
        print("VERDICT: every client was refused.")
        print("  The User-Agent is not the variable. What is left is the")
        print("  source network - a VPN, a corporate egress, or an IP")
        print("  range this WAF refuses. Try the same command on another")
        print("  network (a phone hotspot is the quickest test).")
        print("  The key is still not implicated: DEMO_KEY was refused too.")
        return 1
    if len(ok) == len(results):
        print("VERDICT: every client succeeded, so nothing about the")
        print("  request is being refused now. Either it was transient, or")
        print("  the fault really is the key. Re-run check_techport.py - a")
        print("  JSON 403 naming API_KEY_* would settle it.")
        return 0
    if "descriptive" in ok:
        print("VERDICT: a descriptive User-Agent is accepted and the")
        print("  default one is not. The WAF refuses anonymous")
        print("  programmatic clients.")
        print("  Fix: send a descriptive User-Agent from Client.get().")
        print("  This is honest identification, not evasion - it names the")
        print("  project and a contact, which is what providers ask for.")
        return 0
    print("VERDICT: only a browser User-Agent was accepted.")
    print("  Read the note in this file's docstring before changing")
    print("  anything. A client claiming to be Chrome is a claim that a")
    print("  person is browsing. If that is the only thing this route")
    print("  accepts, the operator is saying it does not want")
    print("  programmatic traffic here - and the answer is to stop and")
    print("  reconsider the source, the same call AD-060 already made.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
