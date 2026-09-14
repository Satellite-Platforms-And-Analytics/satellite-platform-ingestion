"""
One descriptive User-Agent, built in one place.

Why this exists
---------------
On 2026-09-14 the TechPort survey spent a day blocked on a 403 that was
never about the key. api.nasa.gov's WAF refuses `python-requests/2.34.2`
before the request ever reaches api.data.gov's key check, which is why
the body was an Apache HTML page rather than api.data.gov's JSON. A
descriptive User-Agent is accepted; the default one is not. Measured:
403, then 200 with 19,690 projects, changing nothing but that header.

The uncomfortable half of the finding is that **this project already knew
to do this.** `seed_satcat.py` has a `USER_AGENT` constant.
`tle/fetcher.py` hardcodes a string twice. `tracking/satellite_utils.py`
builds one inline twice. Four call sites, three different strings, no
shared definition - so the fifth client, written last, simply did not
have one to reuse. A convention that lives in four copies is a
convention a new caller cannot find.

It is also the politeness the API-usage constraint asks for. A request
that says who is calling, why, and how often is one an operator can
contact instead of block, and every source this project reads - CelesTrak
explicitly - asks for exactly that. Identifying honestly is the opposite
of the browser-impersonation that `check_techport_403.py` declined to
ship.

Deliberately NOT done here
--------------------------
The four existing call sites are left alone today. Two of them are in
`src/tracking/`, the Satellite Visibility Tool, which is in use for work;
rewriting its HTTP headers to tidy a duplication is not a change worth
making on the same day as anything else. Recorded as a finding, to be
done deliberately rather than in passing.
"""
from __future__ import annotations

import os

#: Matches the string seed_satcat.py has been sending since 2026-09-05,
#: which is the one an operator would already have in their logs.
PROJECT = "satellite-platform"
VERSION = "1.0"


def user_agent(purpose: str, rate: str | None = None) -> str:
    """
    Build a User-Agent that names the project, the purpose and the rate.

        satellite-platform/1.0 (catalogue enrichment; 1 req/day)
        satellite-platform/1.0 (technology survey; <=1000 req/hr;
                                contact you@example.com)

    `purpose` says what this particular caller is doing, in the words an
    operator reading a log would want. `rate` states the ceiling the
    caller holds itself to - a claim worth making only where the code
    actually enforces it.

    CONTACT_EMAIL is optional and read from the environment rather than
    committed. An operator who can email you does not have to block you,
    but the address is the user's to publish, not the repository's.
    """
    bits = [purpose]
    if rate:
        bits.append(rate)
    contact = (os.environ.get("CONTACT_EMAIL") or "").strip()
    if contact:
        bits.append(f"contact {contact}")
    return f"{PROJECT}/{VERSION} ({'; '.join(bits)})"
