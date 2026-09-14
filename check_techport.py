"""
Survey NASA TechPort: can it give us the organisation-to-technology edge?

    python check_techport.py                 # ~50 projects, read-only
    python check_techport.py --sample 120

Read-only. Writes nothing to the database and nothing to disk.

WHY THIS RUNS BEFORE ANY IMPORTER IS WRITTEN
============================================
The MVP needs one edge that does not exist: which organisation advances
which technology. `technologies` (69, curated) and `organizations` (4,108,
GCAT) are both keyed and nothing connects them, and unlike everything
built on 2026-09-13 this cannot be answered from files already on disk.

TechPort is the only verified source that carries an organisation, a
taxonomy and a TRL in the same record. Whether it can actually be joined
to what we have is a question with a number for an answer, and the
register's rule is that the number is predicted in writing first.

PREDICTIONS, WRITTEN 2026-09-14 BEFORE THE FIRST REQUEST
========================================================
    projects since 2020         3,000 - 8,000
    with a leadOrganization     > 90%
    with a usable trlCurrent    50 - 70%   (many projects state none)
    with a primaryTx            > 80%

    DISTINCT lead organisations matching an `organizations` row
                                15 - 35%   <-- the decisive number

The last one is the reason for the whole exercise, and the prediction is
low on purpose. TechPort names NASA centres, universities and prime
contractors; GCAT names the organisations that OPERATE objects in orbit.
Those overlap for the centres - GSFC, JPL and JSC are all in GCAT - and
barely at all for a university lab.

**A low number is a finding, not a failure.** It is the difference between
writing an importer tomorrow and writing down that the edge needs a
different source.

RATE LIMITS ARE OBEYED, NOT ASSUMED (AD-060)
============================================
Through `api.nasa.gov`, which publishes 1,000 requests/hour per key and
returns `X-RateLimit-Remaining`, and never through `techport.nasa.gov`
directly, which needs no key and publishes no limit at all. An unmetered
endpoint is not a generous one; it is one where you find the ceiling by
hitting it.

This script reads the remaining quota from every response, prints it, and
stops early if it falls below a floor. A default run costs ~51 requests of
1,000. The standing constraint on this project is that no account is ever
put at risk, and a survey is not worth breaking it for.
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys
import time

from src.env import bootstrap

bootstrap()

import requests                                              # noqa: E402
from sqlalchemy import text                                  # noqa: E402

from src.db.writer import get_engine                         # noqa: E402

BASE = "https://api.nasa.gov/techport/api"

#: Stop if the key's remaining hourly quota falls below this. Leaves room
#: for anything else on the same key and makes the stop a decision rather
#: than a 429.
QUOTA_FLOOR = 200

#: Between requests. 1,000/hour is one every 3.6s sustained; a survey of
#: fifty does not need to sprint, and a polite pace costs 25 seconds.
PAUSE_S = 0.5


#: An api.data.gov key is exactly 40 characters, [A-Za-z0-9]. DEMO_KEY is
#: the one documented exception and is allowed through so the path can be
#: exercised without a key - it will stop on its own at 30 requests/hour.
_KEY_SHAPE = re.compile(r"[A-Za-z0-9]{40}")


def _key() -> str:
    """Return the API key, or fail locally rather than at the server.

    Two things are deliberate here.

    The value is ``.strip()``ed. A key pasted into ``.env`` with a
    trailing space or a stray carriage return is still a 40-character
    key to a human reading the file and a 41-character key to
    api.data.gov, which answers 403 API_KEY_INVALID - an error whose
    text points at the key being *wrong* when it is merely *padded*.

    The shape is checked before any request is made. A truncated paste
    is the most common way this fails, and it costs nothing to say so
    here instead of spending a request to be told the same thing less
    clearly. The key's VALUE is never printed, only its length: this
    message is the kind of thing that ends up in a screenshot.
    """
    k = (os.environ.get("NASA_API_KEY") or "").strip()
    if not k:
        raise SystemExit(
            "NASA_API_KEY is not set in the environment or .env.\n"
            "Register a free key at https://api.nasa.gov (email only).\n\n"
            "DEMO_KEY works for a handful of requests (30/hour per IP) but "
            "not for a survey, and techport.nasa.gov direct is deliberately "
            "not used: no key, and no published rate limit (AD-060).")
    if k != "DEMO_KEY" and not _KEY_SHAPE.fullmatch(k):
        raise SystemExit(
            f"NASA_API_KEY does not have the shape of an api.nasa.gov key.\n"
            f"  expected : 40 characters, letters and digits only\n"
            f"  got      : {len(k)} characters"
            f"{'' if k.isalnum() else ', including punctuation or whitespace'}"
            f"\n\n"
            "If the length is short the paste was truncated. If there is "
            "punctuation, the quotes or a\ntrailing comment from .env came "
            "through with the value. Nothing was sent to the server.")
    return k


class Client:
    def __init__(self, key: str):
        self.key = key
        self.n = 0
        self.remaining: "int | None" = None

    def get(self, path: str, **params):
        if self.remaining is not None and self.remaining < QUOTA_FLOOR:
            raise SystemExit(
                f"stopping: {self.remaining} requests left on this key, "
                f"floor is {QUOTA_FLOOR}. Nothing was written; re-run in "
                f"an hour.")
        params["api_key"] = self.key
        r = requests.get(f"{BASE}{path}", params=params, timeout=30)
        self.n += 1
        rem = r.headers.get("X-RateLimit-Remaining")
        if rem and rem.isdigit():
            self.remaining = int(rem)
        if r.status_code >= 400:
            # THE SERVER'S EXPLANATION IS THE POINT OF THE ERROR.
            #
            # raise_for_status() throws away the response body, so a 403
            # arrived as "403 Client Error: Forbidden" with the actual
            # reason - which api.data.gov puts in a JSON body naming the
            # exact fault - discarded. Guessing at an error the server
            # already explained is the failure mode this project keeps
            # cataloguing under "an error path that destroys the error".
            #
            # The URL is deliberately NOT echoed: requests puts the full
            # query string, api_key included, into its own message, which
            # is how a key ends up in a terminal, a screenshot or a paste.
            detail = ""
            try:
                body = r.json()
                err = body.get("error", body) if isinstance(body, dict) else body
                if isinstance(err, dict):
                    detail = " | ".join(
                        f"{k}: {v}" for k, v in err.items()
                        if k in ("code", "message", "error", "reason"))
                detail = detail or str(body)[:300]
            except Exception:                                # noqa: BLE001
                detail = (r.text or "")[:300]

            hint = ""
            if r.status_code == 403:
                hint = (
                    "\n\n  403 from api.data.gov is almost always the key, "
                    "not the path.\n"
                    "    - API_KEY_INVALID   : the key is wrong or "
                    "mistyped\n"
                    "    - API_KEY_MISSING   : the value came through "
                    "empty\n"
                    "    - API_KEY_DISABLED / _UNAUTHORIZED : registered "
                    "but not usable yet\n\n"
                    "  Check it arrived intact:\n"
                    "    python -c \"import os;k=os.environ.get("
                    "'NASA_API_KEY','');print(len(k), k[:4]+'...'+k[-4:] "
                    "if k else 'EMPTY')\"\n"
                    "  An api.nasa.gov key is 40 characters. If the "
                    "length is wrong the paste was truncated;\n"
                    "  if it is 0 the shell variable did not reach "
                    "python.")
            if r.status_code == 429:
                hint = ("\n\n  The hourly limit was reached. This should "
                        "not happen at this request count - check whether "
                        "the key is shared with something else.")
            raise SystemExit(
                f"HTTP {r.status_code} from api.nasa.gov{path}\n"
                f"  server said: {detail}{hint}")
        time.sleep(PAUSE_S)
        return r.json()


#: Tokens that differ between catalogues without changing the organisation.
_NOISE = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|"
    r"gmbh|plc|sa|ab|bv|the|of|and)\b\.?", re.I)


#: Agency prefixes that one catalogue carries and the other does not.
#: GCAT stores "NASA Goddard Space Flight Center"; TechPort says "Goddard
#: Space Flight Center". Stripping this is a real loosening of the match,
#: so it is reported as its OWN rate rather than folded into the strict
#: one - the survey's job is to say how much the prefix costs, not to
#: quietly buy it.
_AGENCY_PREFIX = re.compile(
    r"^(nasa|esa|jaxa|isro|usaf|us air force|us space force|dod|noaa)\s+")


def norm(s: "str | None") -> str:
    """Lowercase, strip punctuation and corporate noise, collapse spaces."""
    if not s:
        return ""
    s = s.lower().replace("&", " and ")
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_relaxed(s: "str | None") -> str:
    """`norm`, and also without a leading agency name."""
    return _AGENCY_PREFIX.sub("", norm(s)).strip()


def load_org_index(conn) -> tuple:
    """
    Two indexes: strict, and with a leading agency name stripped.

    Kept apart so the survey can report what the looser rule buys. A
    single merged index would report one rate and hide the fact that most
    of it came from a rule nobody has agreed to yet.
    """
    strict: dict = {}
    relaxed: dict = {}
    rows = conn.execute(text("""
        SELECT code, name_english, name_native, name_short
          FROM organizations
    """))
    for code, eng, nat, short in rows:
        for candidate in (eng, nat, short):
            k = norm(candidate)
            if k:
                strict.setdefault(k, code)
            r = norm_relaxed(candidate)
            if r:
                relaxed.setdefault(r, code)
    return strict, relaxed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=50,
                    help="Project details to fetch (default 50).")
    ap.add_argument("--since", default="2020-01-01",
                    help="updatedSince date (default 2020-01-01).")
    args = ap.parse_args(argv)

    c = Client(_key())

    print(f"TechPort survey via api.nasa.gov — read-only\n")
    print(f"  listing projects updated since {args.since} ...")
    listing = c.get("/projects", updatedSince=args.since)

    # The shape of the listing is asserted rather than assumed: a wrapper
    # key that changed would otherwise surface as "0 projects", which
    # looks like an empty catalogue rather than a parsing failure.
    ids = None
    for path in (("projects", "projects"), ("projects",), ("data",)):
        node = listing
        for p in path:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, list):
            ids = node
            break
    if ids is None:
        print("  could not find the id list in the response. Top-level "
              f"keys: {list(listing)[:8]}")
        return 2

    def as_id(x):
        return x.get("projectId") if isinstance(x, dict) else x

    ids = [as_id(x) for x in ids]
    ids = [i for i in ids if i is not None]
    print(f"  projects returned            : {len(ids):,}")
    print(f"  quota remaining              : {c.remaining}")

    take = ids[: args.sample]
    print(f"\n  fetching {len(take)} project details "
          f"({PAUSE_S}s apart) ...")

    have_org = have_trl = have_tx = 0
    org_names: collections.Counter = collections.Counter()
    tx_names: collections.Counter = collections.Counter()
    trls: collections.Counter = collections.Counter()
    detail_shape: collections.Counter = collections.Counter()

    for n, pid in enumerate(take, 1):
        d = c.get(f"/projects/{pid}")
        p = d.get("project", d) if isinstance(d, dict) else {}
        for k in p:
            detail_shape[k] += 1

        lead = p.get("leadOrganization") or {}
        lead_name = lead.get("organizationName") if isinstance(lead, dict) else lead
        if lead_name:
            have_org += 1
            org_names[str(lead_name).strip()] += 1

        trl = p.get("trlCurrent")
        if isinstance(trl, (int, float)) and trl:
            have_trl += 1
            trls[int(trl)] += 1

        tx = p.get("primaryTaxonomyNodes") or p.get("primaryTx")
        if tx:
            have_tx += 1
            if isinstance(tx, list):
                for node in tx:
                    label = (node.get("title") if isinstance(node, dict)
                             else str(node))
                    if label:
                        tx_names[label] += 1
            elif isinstance(tx, dict):
                label = tx.get("title")
                if label:
                    tx_names[label] += 1
        if n % 10 == 0:
            print(f"    {n}/{len(take)}   quota {c.remaining}")

    s = len(take) or 1
    print(f"\n  FIELD COVERAGE over {s} projects")
    print(f"    leadOrganization           : {have_org} ({100*have_org/s:.0f}%)")
    print(f"    trlCurrent                 : {have_trl} ({100*have_trl/s:.0f}%)")
    print(f"    a primary taxonomy node    : {have_tx} ({100*have_tx/s:.0f}%)")
    if trls:
        print(f"    TRL distribution           : "
              f"{dict(sorted(trls.items()))}")

    # ── The decisive number ──────────────────────────────────────────
    engine = get_engine()
    with engine.connect() as conn:
        strict_idx, relaxed_idx = load_org_index(conn)

    print(f"\n  ORGANISATION JOIN  (predicted 15-35% of distinct names)")
    print(f"    names indexed, strict      : {len(strict_idx):,}")
    print(f"    distinct TechPort leads    : {len(org_names):,}")

    matched, by_relaxed, unmatched = {}, {}, []
    for name, count in org_names.items():
        code = strict_idx.get(norm(name))
        if code:
            matched[name] = (code, count)
            continue
        code = relaxed_idx.get(norm_relaxed(name))
        if code:
            by_relaxed[name] = (code, count)
        else:
            unmatched.append((name, count))

    total_names = max(1, len(org_names))
    total_proj = max(1, sum(org_names.values()))
    d_rate = 100 * len(matched) / total_names
    w_matched = sum(cnt for _, cnt in matched.values())
    w_rate = 100 * w_matched / total_proj
    both = len(matched) + len(by_relaxed)
    both_w = w_matched + sum(cnt for _, cnt in by_relaxed.values())

    print(f"    STRICT  distinct names     : {len(matched)} ({d_rate:.0f}%)")
    print(f"    STRICT  weighted by project: {w_matched} ({w_rate:.0f}%)")
    print(f"    +agency-prefix stripped    : +{len(by_relaxed)} names "
          f"-> {both} ({100*both/total_names:.0f}%), "
          f"weighted {both_w} ({100*both_w/total_proj:.0f}%)")
    if by_relaxed:
        print("      (these matched ONLY after dropping a leading agency "
              "name, e.g.")
        for name, (code, cnt) in sorted(
                by_relaxed.items(), key=lambda kv: -kv[1][1])[:4]:
            print(f"       {code:<9}{cnt:>4}  {name[:48]}")
        print("       — a real loosening of the rule, reported separately "
              "so it is a decision)")

    if matched:
        print("\n    matched examples:")
        for name, (code, cnt) in sorted(
                matched.items(), key=lambda kv: -kv[1][1])[:8]:
            print(f"      {code:<10}{cnt:>4}  {name[:52]}")
    if unmatched:
        print("\n    UNMATCHED, most common — read these, they decide it:")
        for name, cnt in sorted(unmatched, key=lambda kv: -kv[1])[:12]:
            print(f"      {'':<10}{cnt:>4}  {name[:52]}")

    if tx_names:
        print(f"\n  TAXONOMY — does it carve the domain like our 13?")
        for label, cnt in tx_names.most_common(12):
            print(f"      {cnt:>4}  {label[:60]}")

    print(f"\n  requests used: {c.n}   quota remaining: {c.remaining}")
    print("\n  Nothing was written. The prediction was 15-35% of distinct")
    print("  names; compare before deciding whether an importer is the")
    print("  right next step or whether this edge needs another source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
