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

RESULT OF RUN 1, 2026-09-14 (--sample-from head, n=50)
======================================================
    projects since 2020        19,690   MISSED HIGH by 2.5x
    with a leadOrganization       64%   MISSED LOW  (predicted >90%)
    with a usable trlCurrent      30%   MISSED LOW  (predicted 50-70%)
    with a primaryTx              56%   MISSED LOW  (predicted >80%)
    DISTINCT orgs matching     3/21 = 14%   just under the 15-35% band

Four predictions out of five wrong, and all four of the coverage ones
wrong in the same direction: TechPort is far emptier, field by field,
than assumed. That consistency is itself the finding - the error was not
noise, it was a wrong model of what a TechPort record contains.

The unmatched list is what actually decides it, and it reads clearly:
Johnson, Kennedy, Ames, Armstrong, JPL - NASA field centres - alongside
Interlune, Thinkorbital, CoolCAD, CFD Research, A10 Systems: SBIR-stage
firms that have never operated a spacecraft. GCAT has no reason to name
any of them. **TechPort names who does the R&D; GCAT names who operates
the hardware.** They are different populations, and the 14% is not a
matching defect to be tuned away.

Note what loosening the rule buys: the two extra matches from stripping
an agency prefix were LARCN (Langley) and MSFC (Marshall) - both NASA
centres. The relaxed rule adds ten points of match rate and almost no
commercial organisations, which is the opposite of what the readiness
story needs.

PREDICTIONS FOR RUN 2, WRITTEN BEFORE RUNNING IT
================================================
Run 1 had two defects of method, both now fixed, and both of which make
its headline number untrustworthy in a *knowable* direction:

  1. It reported three marginal percentages and left the reader to
     multiply them. An edge needs a matched org AND a TRL AND a taxonomy
     node ON THE SAME PROJECT, and multiplying marginals assumes an
     independence nobody checked.
  2. It took `ids[:50]`. The listing is ordered by lastUpdated
     descending, so that is the 50 most recently touched projects -
     which skews hard toward exactly the SBIR awards that cannot match.
     The most adversarial slice available, reported as a property of
     TechPort.

    joint org+TRL+tx on one project (head)   8 - 20%
    ... and org matches strictly (head)      0 - 6%   i.e. 0-3 of 50
    DISTINCT org match, --sample-from spread 20 - 40%
        (higher than head: older projects skew toward large flight
         programmes with operators GCAT knows)
    primaryTx nodes at TX top level          under 50%, mixed depth

If the spread sample also lands in the teens, the population argument is
confirmed and Block 2 does not get written. If it lands at 35%+, run 1
measured its own sampling and not TechPort.

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
import json
import pathlib
import sys
import time

from src.env import bootstrap

bootstrap()

import requests                                              # noqa: E402
from sqlalchemy import text                                  # noqa: E402

from src.db.writer import get_engine                         # noqa: E402
from src.catalog.org_match import (                         # noqa: E402
    load_org_index, match_org, norm, norm_relaxed,
)
from src.useragent import user_agent                         # noqa: E402

BASE = "https://api.nasa.gov/techport/api"

#: NOT COSMETIC. api.nasa.gov's WAF refuses `python-requests/<version>`
#: with an Apache 403, before api.data.gov ever looks at the key - which
#: is what cost 2026-09-14. Measured: 403 with the default header, 200
#: with this one, 19,690 projects, nothing else changed.
#:
#: The rate claim is one the code keeps: QUOTA_FLOOR stops the survey
#: with 200 requests to spare, and PAUSE_S holds it well under the
#: published ceiling.
USER_AGENT = user_agent("technology readiness survey", "<=1000 req/hr")

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


#: api.data.gov's documented general error codes, verified against the
#: developer manual 2026-09-14. Their presence in a body is what proves
#: api.data.gov answered; their absence proves something else did.
API_UMBRELLA_CODES = (
    "API_KEY_MISSING", "API_KEY_INVALID", "API_KEY_DISABLED",
    "API_KEY_UNAUTHORIZED", "API_KEY_UNVERIFIED", "HTTPS_REQUIRED",
    "OVER_RATE_LIMIT", "NOT_FOUND",
)


class Client:
    def __init__(self, key: str):
        self.key = key
        self.n = 0
        self.remaining: "int | None" = None
        self.limit: "int | None" = None
        # A Session so the identifying header cannot be forgotten on one
        # call site, and so fifty requests reuse one connection.
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # THE KEY GOES IN A HEADER, NOT THE QUERY STRING.
        #
        # api.data.gov documents three ways to pass it and says the key
        # "should be kept private". A query parameter is the one that
        # does not keep it: it lands in server logs, in proxy logs, in
        # Referer headers, in browser history, and - the one that bit
        # this project - inside requests' own exception messages, which
        # is why the 403 handler had to be written to suppress the URL.
        # X-Api-Key removes the leak at its source rather than papering
        # over each place it surfaces.
        self.session.headers["X-Api-Key"] = key

    def get(self, path: str, **params):
        if self.remaining is not None and self.remaining < QUOTA_FLOOR:
            raise SystemExit(
                f"stopping: {self.remaining} requests left on this key, "
                f"floor is {QUOTA_FLOOR}. Nothing was written; re-run in "
                f"an hour.")
        r = self.session.get(f"{BASE}{path}", params=params, timeout=30)
        self.n += 1
        rem = r.headers.get("X-RateLimit-Remaining")
        if rem and rem.isdigit():
            self.remaining = int(rem)
        # READ THE CEILING, DO NOT ASSERT IT.
        #
        # The manual says 1,000/hour is the DEFAULT and that "rate limits
        # may vary by service". This key reports ~2,000 on TechPort, so
        # the docstring's 1,000 was a guess that happened to be
        # conservative. The server states the real number on every
        # response; there is no reason to carry a guess alongside it.
        lim = r.headers.get("X-RateLimit-Limit")
        if lim and lim.isdigit():
            self.limit = int(lim)
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
                # WHICH LAYER SAID NO IS THE WHOLE QUESTION, AND THE BODY
                # ANSWERS IT.
                #
                # api.data.gov rejects a key with a JSON body naming the
                # exact fault. Anything in front of it - a WAF, the origin
                # - rejects with Apache's HTML page. The first version of
                # this hint asserted "almost always the key" for both, and
                # on 2026-09-14 the very first real 403 came back as
                # Apache HTML: the request never reached the key check,
                # and the hint sent the reader to inspect a key that was
                # already known to be well formed.
                #
                # An error path that confidently mis-attributes is worse
                # than one that says nothing, because it is believed.
                # WHICH LAYER ANSWERED IS PROVED BY THE ERROR CODE, NOT
                # BY THE CONTENT TYPE.
                #
                # The first version of this branch used Content-Type, on
                # the assumption that HTML meant "not api.data.gov". The
                # developer manual says otherwise: api.data.gov returns
                # its error "in JSON, XML, CSV, or HTML" depending on the
                # detected request format, and its HTML form is
                # <h1>API_KEY_MISSING</h1>. So HTML alone proves nothing.
                #
                # What distinguishes them is the documented code. The
                # Apache page that cost 2026-09-14 carried none of them -
                # only "You don't have permission to access this
                # resource" - which is how we know the request never
                # reached api.data.gov at all.
                body_text = (r.text or "")
                found = [c for c in API_UMBRELLA_CODES if c in body_text]
                if found:
                    hint = (
                        f"\n\n  api.data.gov answered, naming "
                        f"{', '.join(found)}. The request reached the key "
                        f"check, so the key is what was refused.\n"
                        "    - API_KEY_INVALID   : the key is wrong or "
                        "mistyped\n"
                        "    - API_KEY_MISSING   : the value came through "
                        "empty\n"
                        "    - API_KEY_DISABLED / _UNAUTHORIZED : "
                        "registered but not usable\n"
                        "    - API_KEY_UNVERIFIED: registered and the "
                        "confirmation email has not been clicked\n\n"
                        "  Check it arrived intact, without printing it:\n"
                        "    python -c \"import os;k=os.environ.get("
                        "'NASA_API_KEY','');print(len(k))\"\n"
                        "  An api.nasa.gov key is 40 characters.")
                else:
                    hint = (
                        "\n\n  This carries NONE of api.data.gov's "
                        "documented error codes, so it did not come from "
                        "api.data.gov.\n"
                        "  It is a layer in front of it.\n"
                        "  The request was refused BEFORE the key was "
                        "looked at, so the key is not implicated and\n"
                        "  checking it again will not help. Something "
                        "about the request itself was rejected:\n"
                        "  most often the client's User-Agent, sometimes "
                        "the source network.\n\n"
                        "  Isolate it - uses DEMO_KEY only, never your "
                        "key, and spends 4 of its 30/hour:\n"
                        "    python check_techport_403.py")
            if r.status_code == 429:
                hint = ("\n\n  The hourly limit was reached. This should "
                        "not happen at this request count - check whether "
                        "the key is shared with something else.")
            raise SystemExit(
                f"HTTP {r.status_code} from api.nasa.gov{path}\n"
                f"  server said: {detail}{hint}")
        time.sleep(PAUSE_S)
        return r.json()


#: One JSON file per project detail. See data/cache/README.md.
CACHE_DIR = pathlib.Path(__file__).resolve().parent / "data" / "cache" / "techport"

#: A TechPort record's lastUpdated moves on the order of months. A shorter
#: TTL would spend requests to observe nothing.
CACHE_TTL_S = 7 * 24 * 3600


def cached_detail(c: "Client", pid, use_cache: bool = True):
    """
    Fetch one project detail, through a disk cache.

    THE PROJECT HAS A REASON TO CARE ABOUT THIS. On 2026-09-05 it
    re-downloaded 13,517 Space-Track gp_history records for data it
    already held, against a source whose guidance is one request per
    object per lifetime and which had previously suspended the account.
    api.data.gov is more forgiving - it publishes a rate limit rather
    than a caching rule - but "we were allowed to" is not the standard
    this project agreed to work to, and three survey runs had already
    re-fetched the same details three times before this existed.
    """
    f = CACHE_DIR / f"{pid}.json"
    if use_cache and f.is_file():
        age = time.time() - f.stat().st_mtime
        if age < CACHE_TTL_S:
            try:
                return json.loads(f.read_text(encoding="utf-8")), True
            except (OSError, ValueError):
                pass                      # unreadable cache is not an error
    d = c.get(f"/projects/{pid}")
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass                              # a cache we cannot write is not
                                          # a reason to fail the survey
    return d, False


# The matcher moved to src/catalog/org_match.py on 2026-09-15, before its
# second caller existed rather than after. 015 makes
# research_organizations.name_norm a UNIQUE key computed with these exact
# rules, so the importer and this survey must agree on them forever - and
# two implementations of one rule is how they stop agreeing. The same
# lesson the User-Agent produced yesterday, applied one day earlier in
# its life cycle.


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=50,
                    help="Project details to fetch (default 50).")
    ap.add_argument("--since", default="2020-01-01",
                    help="updatedSince date (default 2020-01-01).")
    ap.add_argument("--no-cache", action="store_true",
                    help="Ignore data/cache/techport and refetch every "
                         "detail. Costs one request per project.")
    ap.add_argument("--sample-from", choices=("spread", "head", "tail"),
                    default="spread",
                    help="Which projects to sample. 'spread' takes them "
                         "evenly across the whole listing (default); "
                         "'head' takes the most recently updated, which "
                         "is what the first survey did and is the most "
                         "adversarial slice; 'tail' the least recently.")
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

    # HOW THE SAMPLE IS DRAWN IS PART OF THE RESULT.
    #
    # The first survey took ids[:50] and reported a 14% organisation
    # match as though it were a property of TechPort. The listing comes
    # back ordered by lastUpdated descending, so those 50 were the 50
    # most recently touched projects - which skews hard toward active
    # SBIR/STTR awards to small firms that have never operated a
    # spacecraft and that GCAT therefore has no reason to name. That is
    # the single most adversarial slice available for this particular
    # question, and nothing in the output said so.
    #
    # 'spread' walks the whole listing at a fixed stride for the same
    # number of requests. It is not a random sample - it is a systematic
    # one - but it is not concentrated in one edge of the distribution.
    n_ids = len(ids)
    if args.sample >= n_ids or args.sample_from == "head":
        take = ids[: args.sample]
    elif args.sample_from == "tail":
        take = ids[-args.sample:]
    else:
        stride = n_ids / args.sample
        take = [ids[int(k * stride)] for k in range(args.sample)]
    print(f"  sampling                     : {args.sample_from} "
          f"({len(take)} of {n_ids:,}"
          + (f", stride {n_ids / args.sample:.0f}"
             if args.sample_from == "spread" and args.sample < n_ids
             else "") + ")")
    print(f"\n  fetching {len(take)} project details "
          f"({PAUSE_S}s apart) ...")

    records: list[dict] = []
    detail_shape: collections.Counter = collections.Counter()

    from_cache = 0
    for n, pid in enumerate(take, 1):
        d, hit = cached_detail(c, pid, use_cache=not args.no_cache)
        from_cache += hit
        p = d.get("project", d) if isinstance(d, dict) else {}
        for k in p:
            detail_shape[k] += 1

        lead = p.get("leadOrganization") or {}
        lead_name = (lead.get("organizationName")
                     if isinstance(lead, dict) else lead)
        lead_name = str(lead_name).strip() if lead_name else None

        trl = p.get("trlCurrent")
        trl = int(trl) if isinstance(trl, (int, float)) and trl else None

        # KEEP THE CODE, NOT ONLY THE TITLE.
        #
        # The first version took node["title"] and dropped everything
        # else, which made "are these nodes at the same depth?" -
        # the question that decides whether they can be grouped at all -
        # unanswerable from the survey's own output. TX08 and
        # TX08.2.4.3 are both titles; only the code says which is which.
        tx_pairs = []
        tx = p.get("primaryTaxonomyNodes") or p.get("primaryTx")
        nodes = tx if isinstance(tx, list) else ([tx] if tx else [])
        for node in nodes:
            if isinstance(node, dict):
                code = (node.get("code") or node.get("taxonomyNumber")
                        or node.get("number") or "")
                title = node.get("title") or ""
            else:
                code, title = "", str(node)
            if title or code:
                tx_pairs.append((str(code).strip(), str(title).strip()))

        records.append({"id": pid, "org": lead_name, "trl": trl,
                        "tx": tx_pairs})
        if n % 10 == 0:
            print(f"    {n}/{len(take)}   quota {c.remaining}"
                  f"   cached {from_cache}")

    s = len(records) or 1
    have_org = sum(1 for r in records if r["org"])
    have_trl = sum(1 for r in records if r["trl"])
    have_tx = sum(1 for r in records if r["tx"])
    trls = collections.Counter(r["trl"] for r in records if r["trl"])
    org_names = collections.Counter(r["org"] for r in records if r["org"])

    print(f"\n  FIELD COVERAGE over {s} projects  [sample: {args.sample_from}]")
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

    # One matcher, shared with the importer. The local copy this replaces
    # labelled the loose match "relaxed" while 015 and the importer call
    # it "agency_prefix_stripped" - a disagreement that would have
    # reached the database as two different values of match_method for
    # one rule, which is precisely the drift the shared module exists to
    # prevent, already happening inside a single file.
    def match(name):
        return match_org(name, strict_idx, relaxed_idx)

    for r in records:
        r["code"], r["how"] = match(r["org"])

    print(f"\n  ORGANISATION JOIN  (predicted 15-35% of distinct names)")
    print(f"    names indexed, strict      : {len(strict_idx):,}")
    print(f"    distinct TechPort leads    : {len(org_names):,}")

    matched, by_relaxed, unmatched = {}, {}, []
    for name, count in org_names.items():
        code, how = match(name)
        if how == "strict":
            matched[name] = (code, count)
        elif how == "agency_prefix_stripped":
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

    # ── WHAT AN EDGE ACTUALLY NEEDS: ALL THREE AT ONCE ───────────────
    #
    # The first survey reported three marginal percentages and left the
    # reader to multiply them, which assumes independence nobody checked.
    # An edge in 015 needs a matched organisation AND a TRL AND a
    # taxonomy node ON THE SAME PROJECT. That is one number and it is
    # the only one that decides anything.
    def n_with(pred):
        return sum(1 for r in records if pred(r))

    strict_ok = n_with(lambda r: r["how"] == "strict")
    any_ok = n_with(lambda r: r["how"] in ("strict", "agency_prefix_stripped"))
    usable_strict = n_with(lambda r: r["how"] == "strict" and r["trl"]
                           and r["tx"])
    usable_any = n_with(lambda r: r["how"] in ("strict", "agency_prefix_stripped")
                        and r["trl"] and r["tx"])
    all_three_any_org = n_with(lambda r: r["org"] and r["trl"] and r["tx"])

    print(f"\n  JOINT COVERAGE — the number an edge table actually needs")
    print(f"    org + TRL + taxonomy, any org name : "
          f"{all_three_any_org}/{s} ({100*all_three_any_org/s:.0f}%)")
    print(f"    ... and the org matches STRICTLY   : "
          f"{usable_strict}/{s} ({100*usable_strict/s:.0f}%)")
    print(f"    ... allowing the relaxed match too : "
          f"{usable_any}/{s} ({100*usable_any/s:.0f}%)")
    print(f"    (projects whose org matched at all : "
          f"{strict_ok} strict, {any_ok} incl. relaxed)")
    if usable_strict:
        print("\n    the usable ones, in full:")
        for r in records:
            if r["how"] == "strict" and r["trl"] and r["tx"]:
                code, title = r["tx"][0]
                print(f"      {r['code']:<9} TRL{r['trl']:<3} "
                      f"{(code + ' ') if code else ''}{title[:38]}")
                print(f"      {'':<9} {r['org'][:60]}")

    # ── TAXONOMY: SAME CARVE-UP, OR A DIFFERENT AXIS? ────────────────
    tx_codes = collections.Counter()
    tx_names = collections.Counter()
    depths = collections.Counter()
    for r in records:
        for code, title in r["tx"]:
            tx_names[title] += 1
            if code:
                tx_codes[code] += 1
                depths[code.count(".") + 1] += 1
            else:
                depths["no code"] += 1

    if tx_names:
        print(f"\n  TAXONOMY — does it carve the domain like our 13?")
        print(f"    node depth distribution    : {dict(depths)}")
        print("    (1 = a TX top-level area. Anything deeper cannot be")
        print("     grouped with a top-level one without rolling it up")
        print("     first, and a mixed-depth column silently will not.)")
        for (code, title), cnt in collections.Counter(
                (c_, t_) for r in records for c_, t_ in r["tx"]
        ).most_common(14):
            print(f"      {cnt:>4}  {(code or '—'):<10} {title[:48]}")

    if detail_shape:
        print(f"\n  DETAIL FIELDS PRESENT (top 20 of {len(detail_shape)})")
        print("    collected since the first survey and never printed, "
              "which is")
        print("    the same defect as the discarded response body:")
        row = []
        for k, cnt in detail_shape.most_common(20):
            row.append(f"{k}({cnt})")
        for k in range(0, len(row), 3):
            print("      " + "  ".join(f"{x:<26}" for x in row[k:k + 3]))

    print(f"\n  requests used: {c.n}   served from cache: {from_cache}")
    print(f"  quota remaining: {c.remaining}"
          + (f" of {c.limit} (the server's number, not ours)"
             if c.limit else ""))
    print("\n  Nothing was written. Read JOINT COVERAGE, not the three")
    print("  marginal percentages: an edge needs all three on one project,")
    print("  and multiplying the marginals assumes an independence that")
    print("  nobody has checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
