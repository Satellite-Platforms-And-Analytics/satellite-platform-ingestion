"""
SEC EDGAR company facts -> `cost_claims`.

    python -m src.funding.sec_edgar --survey
    python -m src.funding.sec_edgar --apply

Second money source. The first one, USAspending, cannot populate
`cost_claim_conflicts` by itself: a conflict needs two sources naming
the same subject with different values, and one source has nothing to
disagree with. This is the source that makes that view capable of
firing.

WHY THIS SOURCE IS EASIER THAN THE LAST ONE
===========================================
EDGAR hands out a **CIK** -- a stable numeric identifier. So this module
does no name matching at all, and that is a deliberate rejection of the
approach that cost most of 2026-10-12 (AD-108, AD-110). The CIK -> org
mapping lives in `data/seed/sec_ciks.tsv`, decided by a person who has
read the survey, and `--apply` refuses any row still marked `pending`.

RATE LIMITS (AD-060, docs/API_USAGE_POLICY.md)
==============================================
Read at the source on 2026-10-12, and the first draft of this file cited
it wrongly.

**10 requests/second**, stated on the webmaster FAQ: *"Note that our
current maximum access rate is 10 requests per second"*, "carefully
monitored to preserve equitable access for all users". It is NOT on the
EDGAR APIs page, which says only that the APIs "do not require any
authentication or API keys to access" and states no limit at all. An
earlier version of this docstring credited the APIs page for the
number -- the same error as quoting a rate limit for USAspending that
nobody had published, run in reverse.

This module holds itself to **5/s**, half the stated ceiling, because a
ceiling is not a target and nothing here is urgent.

THE USER-AGENT IS A POLICY CHOICE HERE, NOT A DOCUMENTED MANDATE
----------------------------------------------------------------
The FAQ says *"Please declare your user agent in request headers"* and
gives the sample `Sample Company Name AdminContact@<domain>.com`. It
**does not say a contact address is required**, and it does not say what
happens if you exceed the limit. What it does mention is an "Undeclared
Automated Tool" error, so an undeclared client can evidently be refused.

`src/useragent.py` always declares the project, version and purpose, so
this client is never undeclared. Only the contact address is
conditional, and `CONTACT_EMAIL` is optional in this project and ships
blank in `.env.example`.

This module still refuses to run without it. That is a **choice**, made
for the reason `useragent.py`'s own docstring gives -- an operator who
can email you does not have to block you -- and for the standing
instruction on this project not to risk a hold on any source. It is not
a claim that EDGAR demands it. The Data Source Register's line that
"useragent.py and CONTACT_EMAIL already satisfy this" overstated the
requirement in the same direction.

WHICH ENDPOINT, AND WHY NOT THE CHEAPER ONE
===========================================
The APIs page documents four, and the first draft of this module used
the wrong one.

`companyfacts/CIK##########.json` returns EVERY concept a company has
ever reported -- megabytes, when this module wants exactly two tags. The
per-company fetch now uses `companyconcept/CIK##########/us-gaap/<tag>`
instead: one concept for one company, a small payload, and a cache that
stays small. Two requests per filer rather than one, and far less of
everything else. `companyconcept` also carries `entityName`, so `apply`
no longer needs a `submissions` call per filer at all.

`frames/us-gaap/<tag>/USD/CY2024.json` returns **one fact for every
reporting entity** for that concept and period -- one request where this
module would otherwise make sixteen. It is the right tool for
DISCOVERY, and `--frames` uses it for exactly that: finding which filers
report a concept at all, and cross-checking what we wrote.

**It is the wrong tool for the claim itself**, and the reason is
AD-107. The docs say a frame "aggregates one fact for each reporting
entity that is last filed that **most closely fits** the calendrical
period requested", with a tolerance of +/-30 days on an annual period.
That hands the choice of VINTAGE to an opaque fitting rule -- and
`as_of_date` is part of this table's natural key, three hours after a
vintage error nearly stamped a 2026 total with a 1993 date. Period
assignment has to be ours, stated, and visible in the code. So frames
informs and never writes.

`companyfacts.zip` and `submissions.zip` are **recompiled nightly** and
hold the entire dataset. That is the honest answer for a real backfill
-- zero per-company requests -- and it is the same conclusion reached
for USAspending: the per-entity path is for sampling, bulk is for
loading history. Not built yet, recorded so the next person does not
re-derive it.

AN ORDERED CHAIN OF TAGS, AND source_url NAMES THE ONE THAT WAS READ
===================================================================
Revenue is reported under several XBRL tags -- `Revenues`,
`SalesRevenueNet`, `RevenueFromContractWithCustomerExcludingAssessedTax`
and more. This module read exactly one of them until 2026-10-19, on the
argument that falling back would make `source='sec_edgar'` plus
`metric='revenue'` silently mean different things per company.

The first live `--survey` refuted the rule and confirmed the argument at
the same time. Lockheed and Boeing reported
`RevenueFromContractWithCustomer...` for 2017-2019 and `Revenues`
thereafter, so the one-tag rule would have written **$59.8bn as
Lockheed's revenue, correctly stamped `as_of 2019-12-31`**, against a
real 2025 figure near $74bn. That is not a gap; it is a stale number
wearing a valid date, which is the failure this project keeps meeting
(AD-113, AD-116).

So: an ORDERED CHAIN per metric, in `TAGS_FOR_METRIC`. `pick_series()`
reads the chain in order and returns the first tag whose newest annual
fact is within `STALE_SERIES_DAYS` of today. It never merges two tags:
one tag supplies the whole series, because a spliced series is one no
filer ever reported and no URL could cite.

What made the chain safe was fixing the thing the old objection named.
`source_url` used to point at a per-filer browse-edgar page, which
records nothing; it now points at the **companyconcept URL actually
read**, and that URL CONTAINS THE TAG. The heterogeneity is still
there -- it is EDGAR's, not ours -- but every row now says which
concept it came from. Silent heterogeneity was the real objection;
named heterogeneity is just provenance.

ANNUAL ONLY
===========
Facts are kept only when `fp == 'FY'`, the form is a 10-K, and the
period spans at least 300 days. Mixing a quarter into a series of
annuals produces a number four times too small with nothing to mark it.
The survey reports how many facts each rule rejected.

WHAT IS NOT FILED
=================
**`market_cap`.** 021 has the metric and EDGAR cannot supply it: market
capitalisation is price times shares, EDGAR carries shares outstanding
and no price at all. Computing it would need a second source, would make
the claim `derived` rather than `published`, and would need a
`derivation_basis` naming the price and its date. Out of scope here, and
recorded so nobody fills it in from a stale quote.

VINTAGE
=======
This is the first source where `as_of_date` and `published_date`
genuinely differ. A fiscal year's revenue is ABOUT the period end; the
filing states it months later. `as_of = end`, `published = filed`, both
from EDGAR, so `is_forecast` stays false by construction (a 10-K cannot
be filed before its period closes) and AD-107's distinction finally has
two different real dates in it.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
from collections import Counter
from datetime import date, datetime

from src.env import bootstrap

bootstrap()

from sqlalchemy import text                                  # noqa: E402

from src.db.writer import get_engine                         # noqa: E402
from src.useragent import user_agent                         # noqa: E402

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
#: What a seed row's `decision` column may say.
#:
#:   pending  undecided. --apply refuses the file while any row says this
#:   match    in scope AND mapped to an existing `organizations` row
#:   claim    in scope; needs an organizations row or a match
#:   refuse   out of scope, reason in the basis
#:   consume  ITS BUSINESS DEPENDS ON A SPACE SERVICE IT DOES NOT SUPPLY
#:
#: `consume` was added 2026-10-19 at the user's request, and it is not a
#: softer refusal - it is a different axis. The other four answer "is
#: this part of the space industry". `consume` answers "does this
#: company's business rest on something in orbit", which is the DEMAND
#: side: Garmin needs GNSS it does not own, T-Mobile resells Starlink
#: capacity, Qualcomm ships modems that talk to satellites. Counting
#: those, and eventually their spend, is how the platform can say
#: something about demand in a segment rather than only about supply.
#:
#: WHAT `consume` MUST NOT DO: a consume row's revenue is NOT space
#: revenue. Lockheed's revenue is money the space industry earned;
#: Garmin's is money consumers paid for handheld GPS units, of which
#: some unknown fraction reflects the value of a space service. Writing
#: the second into cost_claims as though it were the first would be the
#: worst kind of category error - a real number meaning something other
#: than its column says. So --apply ingests `match` and `claim` only,
#: and `consume` rows are recorded and skipped until there is a model
#: that can state what their money measures.
#:
#: AND A LIMIT WORTH KNOWING: this category can never be populated by
#: an SIC scan. Consumers of space services are classified under THEIR
#: OWN industry - an airline under air transport, a farm-equipment maker
#: under machinery, an insurer under insurance - so the nine in-scope
#: codes will only ever catch the handful of consumers that happen to
#: sit in communications or navigation. The three found here are a
#: start, not a population.
DECISIONS = ("pending", "match", "claim", "refuse", "consume")

#: Decisions `--apply` will actually ingest financial facts for.
INGESTABLE = ("match", "claim")

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CONCEPT = ("https://data.sec.gov/api/xbrl/companyconcept/"
           "CIK{cik:010d}/us-gaap/{tag}.json")
FRAMES = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{year}.json"
#: Kept for reference only. Returns every concept a filer has ever
#: reported; companyconcept returns the two this module wants.
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
#: A human's entry point to a filer's filings. NOT `source_url`: it
#: names the company and records nothing about which concept was read,
#: which is precisely why the tag chain was unsafe while claims pointed
#: here. Kept because the next person will look for it, and because
#: writing it into a claim again should look like the regression it is.
FILING_URL = ("https://www.sec.gov/cgi-bin/browse-edgar"
              "?action=getcompany&CIK={cik:010d}")

#: EDGAR publishes 10/s. We take half of it.
MIN_INTERVAL_S = 0.2
DEFAULT_MAX_REQUESTS = 60

SEED = (pathlib.Path(__file__).resolve().parent.parent.parent
        / "data" / "seed" / "sec_ciks.tsv")
CACHE_DIR = (pathlib.Path(__file__).resolve().parent.parent.parent
             / "data" / "cache" / "sec_edgar")
CACHE_TTL_S = 7 * 24 * 3600

#: The ORDERED chain of XBRL tags each metric may come from, preferred
#: first. ONE definition, read in order by pick_series(); nothing is
#: derived from it, so there is no second list to drift.
#:
#: WHY THIS REPLACED A SINGLE TAG, 2026-10-19
#: ------------------------------------------
#: The one-tag rule was deliberate and it was wrong in a way only a run
#: could show. The first --survey against the live seed reported:
#:
#:     LMT  revenue  3 annual, 2017-12-31 .. 2019-12-31  $59,812,000,000
#:     BA   revenue  3 annual, 2017-12-31 .. 2019-12-31  $76,559,000,000
#:
#: Lockheed and Boeing used RevenueFromContractWithCustomer... briefly
#: around 2018-19 and then reported `Revenues` instead. --apply would
#: have written $59.8bn as Lockheed's revenue, correctly stamped
#: as_of 2019-12-31, against a real 2025 figure near $74bn. That is
#: worse than a gap: a stale number wearing a valid date, which is the
#: shape this whole project keeps finding (AD-113, AD-116).
#:
#: The objection to a chain was that "cost_claims has no column
#: recording which tag a row came from", so source='sec_edgar' plus
#: metric='revenue' would mean different things per filer. That was
#: wrong in a fixable way: source_url was pointing at a per-filer
#: browse-edgar page rather than at the companyconcept URL actually
#: read -- and that URL CONTAINS THE TAG. Pointing it at what was
#: fetched records the choice for free, with no migration, and the
#: heterogeneity stops being silent. Silent heterogeneity was the real
#: objection; named heterogeneity is just provenance.
TAGS_FOR_METRIC = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet"),
    "rd_spend": ("ResearchAndDevelopmentExpense",
                 "ResearchAndDevelopmentExpenseExcludingAcquiredInProcess"
                 "Cost"),
}

#: How far behind today a series may end before a fallback is tried.
#: 550 days is about eighteen months: long enough that a filer who has
#: simply not filed this year's 10-K yet is left alone, short enough
#: that a series abandoned in 2019 is caught.
#:
#: A THRESHOLD WITH A VISIBLE AUTHOR (AD-115). Nothing published says
#: eighteen months; this project chose it, and it belongs in one named
#: place rather than inline where it would read as a fact about EDGAR.
STALE_SERIES_DAYS = 550

NOT_FILED = {
    "market_cap": "EDGAR has shares outstanding and no price. Market cap "
                  "would be derived from a second source and needs a "
                  "derivation_basis naming the price and its date.",
}

#: The SEC's in-scope SIC codes. MOVED to src/sic.py on 2026-10-14 so
#: that src/feeds/watch.py could read the same nine codes without
#: importing this module, which pulls in src.db.writer and sqlalchemy.
#: The alternative was a second copy of the list, which is AD-112 with
#: the serial numbers filed off. Re-exported here because this module's
#: own tests and docstrings refer to it by this name.
#:
#: Here it is REPORTED, never used to filter: a filer is in the seed
#: because a person put it there with a written basis.
from src.sic import SPACE_SIC                                 # noqa: E402


def _brief_body(raw: bytes) -> str:
    """What to keep from an HTTP error body: the server's own reason.

    Both directions of this were wrong inside one day. `str(exc)[:50]`
    cut a 404 off at `.../api/xbrl/compan` -- the problem named and the
    URL needed to reproduce it gone. Printing the body whole then put
    300 characters of S3 XML into the middle of the trail it was printed
    into, carrying a `RequestId` and `HostId` nobody will ever look up
    and a `Key` already present in the URL beside it.

    So: `<Code>` and `<Message>` when the body is one of EDGAR's S3
    errors, otherwise the body collapsed onto one line and capped. One
    line, because the caller prints it as one.
    """
    body = raw.decode("utf-8", "replace")
    found = [m.group(1).strip() for m in (
        re.search(r"<Code>([^<]{0,80})</Code>", body),
        re.search(r"<Message>([^<]{0,200})</Message>", body)) if m]
    if found:
        return ": ".join(found)
    return " ".join(body.split())[:200]


class Client:
    def __init__(self, max_requests: int = DEFAULT_MAX_REQUESTS):
        contact = (os.environ.get("CONTACT_EMAIL") or "").strip()
        if not contact:
            raise SystemExit(
                "CONTACT_EMAIL is not set.\n\n"
                "The SEC's webmaster FAQ asks callers to declare a user "
                "agent and shows a sample\ncarrying an admin contact "
                "address. It does NOT state that the address is required, "
                "so\nthis refusal is THIS PROJECT'S choice, not EDGAR's "
                "rule: an operator who can email\nyou does not have to "
                "block you, and the standing instruction here is not to "
                "risk a\nhold on any source.\n\n"
                "Set CONTACT_EMAIL in .env and re-run. No request has been "
                "made.")
        self.n = 0
        self.max_requests = max_requests
        self._last = 0.0
        self.ua = user_agent("space-industry financial research",
                             rate="<=5 req/s against a published 10/s")

    def get(self, url: str) -> dict:
        if self.n >= self.max_requests:
            raise RuntimeError(
                f"request cap reached ({self.max_requests}). Raise it with "
                "--max-requests if that is deliberate; it exists so a bug "
                "cannot spend hundreds of calls unattended (AD-099).")
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        })
        self.n += 1
        self._last = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            why = _brief_body(e.read())
            raise RuntimeError(
                f"HTTP {e.code} from {url}"
                + (f" -- {why}" if why else "")) from None


def cached(c: Client, url: str, key: str, use_cache: bool = True):
    """-> (payload, from_cache)."""
    f = CACHE_DIR / f"{key}.json"
    if use_cache and f.is_file():
        if time.time() - f.stat().st_mtime < CACHE_TTL_S:
            try:
                return json.loads(f.read_text(encoding="utf-8")), True
            except Exception:                                # noqa: BLE001
                pass
    payload = c.get(url)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass
    return payload, False


def read_seed() -> list:
    """-> [(ticker, org_code, decision, basis)]. Refuses a thin basis."""
    if not SEED.is_file():
        raise SystemExit(f"missing seed file: {SEED}")
    out = []
    for line in SEED.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if parts[0].lower() == "ticker":
            continue
        if len(parts) < 4:
            raise SystemExit(f"seed row has no basis column: {line[:60]}")
        tk, code, decision, basis = (parts[0].strip(), parts[1].strip(),
                                     parts[2].strip(), parts[3].strip())
        if decision not in DECISIONS:
            raise SystemExit(
                f"seed row {tk}: bad decision {decision!r}. "
                f"One of {', '.join(sorted(DECISIONS))}.")
        if len(basis) <= 40:
            raise SystemExit(
                f"seed row {tk}: basis too thin to review ({basis!r}). "
                "Same rule as org_aliases.tsv.")
        out.append((tk, code, decision, basis))
    return out


def _d(v) -> "date | None":
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def annual_facts(payload: dict, tag: str) -> "tuple[list, Counter]":
    """Annual USD facts for one tag, plus why each rejection happened.

    Accepts either shape: a `companyconcept` payload, where `units` is
    at the top level, or a `companyfacts` payload, where it is buried
    under facts -> us-gaap -> <tag>. One function so the annual rules
    cannot drift between the two callers.

    The filtering is DELIBERATELY ours rather than the frames API's.
    See "WHICH ENDPOINT" in the module docstring: a frame picks the fact
    that "most closely fits" a calendar period, and vintage is part of
    this table's key.
    """
    why: Counter = Counter()
    units = payload.get("units")
    if units is None:
        node = (payload.get("facts", {}).get("us-gaap", {}).get(tag) or {})
        units = node.get("units", {})
    if not units:
        why["tag absent"] += 1
        return [], why
    rows = units.get("USD")
    if not rows:
        why[f"no USD unit (has {sorted(units)[:3]})"] += 1
        return [], why
    keep = []
    for f in rows:
        if f.get("fp") != "FY":
            why["not fp=FY"] += 1
            continue
        if not str(f.get("form", "")).startswith("10-K"):
            why[f"form {f.get('form')}"] += 1
            continue
        start, end = _d(f.get("start")), _d(f.get("end"))
        if not start or not end:
            why["no start/end"] += 1
            continue
        if (end - start).days < 300:
            why[f"span {(end - start).days}d"] += 1
            continue
        if f.get("val") is None:
            why["null val"] += 1
            continue
        keep.append(f)
    # One fact per period end; a restatement filed later supersedes.
    best: dict = {}
    for f in keep:
        end = _d(f["end"])
        prev = best.get(end)
        if prev is None or (_d(f.get("filed")) or date.min) > (
                _d(prev.get("filed")) or date.min):
            if prev is not None:
                why["superseded by a later filing"] += 1
            best[end] = f
    return [best[k] for k in sorted(best)], why


def tag_from_url(url: str) -> "str | None":
    """The XBRL tag a companyconcept `source_url` names, or None.

    A claim's source_url is the URL that was actually read, so the tag
    it came from is recoverable FROM THE ROW rather than recalled from
    a constant that may since have changed (AD-112). Returns None for
    anything that is not a companyconcept URL -- notably the
    browse-edgar pages written before 2026-10-19, which record no tag
    at all and must read as "not knowable", never as a tag.
    """
    s = str(url or "")
    if "/us-gaap/" not in s or not s.endswith(".json"):
        return None
    return s.rsplit("/us-gaap/", 1)[-1][:-len(".json")] or None


def pick_series(c: Client, cik: int, metric: str, use_cache: bool,
                today: "date | None" = None) -> dict:
    """Choose ONE tag's annual series for one filer, by reading the chain.

    Walks `TAGS_FOR_METRIC[metric]` in preference order and returns the
    first tag whose newest usable annual fact is no more than
    `STALE_SERIES_DAYS` behind `today`. If every tag that has data is
    older than that, the MOST PREFERRED of those is returned with
    `stale_days` set: an old fact carrying its own `as_of_date` is still
    true, and silently preferring a less-preferred tag because it
    happens to be newer would be a choice nobody could see.

    IT NEVER MERGES TWO TAGS. See the module docstring: one tag supplies
    the whole series, and the tag comes back with it so the caller can
    record it in `source_url`.

    Returns a dict: `tag` (None when nothing was usable), `facts` (that
    one tag's annual facts, oldest first), `why` (that tag's rejection
    counter), `entity_name`, `stale_days`, and `trail` -- one line per
    tag tried, in order, so a reader sees the DECISION and not only its
    result. A fetch failure is a trail line, not an exception: a 404 on
    the preferred tag is exactly the case the chain exists for.
    """
    today = today or date.today()
    trail: list = []
    fallback = None
    for tag in TAGS_FOR_METRIC[metric]:
        try:
            cc, _h = cached(c, CONCEPT.format(cik=cik, tag=tag),
                            f"cc_{cik:010d}_{tag}", use_cache)
        except RuntimeError as exc:
            trail.append(f"{tag}: not fetched -- {exc}")
            continue
        keep, why = annual_facts(cc, tag)
        if not keep:
            trail.append(f"{tag}: no usable annual fact"
                         + (f" (rejected {dict(why)})" if why else ""))
            continue
        end = _d(keep[-1]["end"])
        stale_days = (today - end).days if end else None
        found = {"tag": tag, "facts": keep, "why": why,
                 "entity_name": cc.get("entityName"),
                 "stale_days": stale_days, "trail": trail}
        if stale_days is not None and stale_days <= STALE_SERIES_DAYS:
            trail.append(f"{tag}: {len(keep)} annual, newest {keep[-1]['end']}"
                         f" ({stale_days}d old) -- USED")
            return found
        trail.append(f"{tag}: {len(keep)} annual, newest {keep[-1]['end']} "
                     f"({stale_days}d old, over {STALE_SERIES_DAYS}) -- "
                     f"passed over for a newer tag")
        if fallback is None:
            fallback = found
    if fallback is not None:
        trail.append(f"no tag in the chain is within {STALE_SERIES_DAYS} "
                     f"days; keeping {fallback['tag']}, the most preferred "
                     f"tag that has any data at all")
        return fallback
    return {"tag": None, "facts": [], "why": Counter(), "entity_name": None,
            "stale_days": None, "trail": trail}


def resolve_tickers(c: Client, wanted: list, use_cache: bool) -> dict:
    """ticker -> cik, from EDGAR's own file. One request for all of them."""
    payload, hit = cached(c, TICKERS_URL, "company_tickers", use_cache)
    by_ticker = {}
    for row in payload.values():
        by_ticker[str(row.get("ticker", "")).upper()] = int(row["cik_str"])
    print(f"  company_tickers.json: {len(by_ticker)} tickers "
          f"({'cached' if hit else 'fetched'})")
    return {t: by_ticker.get(t.upper()) for t in wanted}


def survey(c: Client, use_cache: bool, limit: int) -> int:
    seed = read_seed()
    print(f"\n  seed rows: {len(seed)}   "
          f"{dict(Counter(d for _t, _o, d, _b in seed))}")
    print(f"  User-Agent: {c.ua}")

    ciks = resolve_tickers(c, [t for t, _o, _d_, _b in seed], use_cache)
    missing = [t for t, v in ciks.items() if not v]
    if missing:
        print(f"\n  NOT IN EDGAR'S TICKER FILE: {missing}")
        print("    A ticker EDGAR does not know is a ticker I got wrong, "
              "or a company that\n    has delisted. Either way it is a "
              "seed-file correction, not a code change.")

    print(f"\n  IDENTITY AND SIC, FROM EDGAR ITSELF")
    print(f"  {'ticker':<8}{'cik':<12}{'sic':<7}{'name as EDGAR has it'}")
    facts_for = {}
    shown = 0
    for tk, code, decision, _basis in seed:
        cik = ciks.get(tk)
        if not cik or decision == "refuse":
            continue
        sub, _hit = cached(c, SUBMISSIONS.format(cik=cik),
                           f"sub_{cik:010d}", use_cache)
        sic = str(sub.get("sic") or "")
        flag = ("" if sic in SPACE_SIC
                else "   <-- SIC not in the in-scope list")
        if decision == "consume":
            # Surveyed on purpose: what SIC the SEC gives a demand-side
            # filer is exactly the thing the consume category is for.
            flag += "   [consume: demand side, not ingested]"
        print(f"  {tk:<8}{cik:<12}{sic:<7}{str(sub.get('name'))[:40]}{flag}")
        if shown < limit:
            facts_for[tk] = (cik, sub.get("name"))
            shown += 1

    # COVERAGE, VIA frames -- one request per tag for EVERY filer in the
    # country, instead of one request per filer. This is what frames is
    # for, and the only thing this module trusts it with: it answers "who
    # reports this tag at all", never "what is the value for a period".
    year = date.today().year - 1
    print(f"\n  WHO REPORTS WHAT, from frames CY{year} "
          f"(1 request per tag, not per filer)")
    cik_to_tk = {v: k for k, v in ciks.items() if v}
    reports: dict = {}
    for metric, chain in TAGS_FOR_METRIC.items():
        for t in chain:
            try:
                fr, _h = cached(c, FRAMES.format(tag=t, year=year),
                                f"frame_{t}_{year}", use_cache)
            except RuntimeError as exc:
                if "HTTP 404" in str(exc):
                    # A frame exists per tag PER YEAR. A concept nobody
                    # reported in CY<year> has no frame for it, and that
                    # is an absence, not a fault -- SalesRevenueNet is a
                    # pre-2018 tag and 404s here every run. Saying
                    # "unavailable" would invite the reading that the
                    # tag is broken, which is AD-113's shape.
                    print(f"    {t:<62}no CY{year} frame: nobody reported "
                          f"this concept that year.")
                    print(f"      Says nothing about the historical "
                          f"series; companyconcept decides that.")
                else:
                    print(f"    {t}: frame unavailable -- {exc}")
                continue
            here = {int(d["cik"]) for d in fr.get("data", [])}
            for cik in here & set(cik_to_tk):
                reports.setdefault(cik_to_tk[cik], set()).add(t)
            print(f"    {t:<62}{len(fr.get('data', [])):>6} filers "
                  f"nationwide, {len(here & set(cik_to_tk))} of ours")

    print(f"\n  {'ticker':<8}{'metric':<10}{'preferred?':<12}"
          f"other chain tags it reports")
    for tk, _code, decision, _b in seed:
        if decision not in INGESTABLE or not ciks.get(tk):
            continue        # consume rows are never written as claims
        for metric, chain in TAGS_FOR_METRIC.items():
            got = reports.get(tk, set())
            ours = "yes" if chain[0] in got else "NO"
            alt = [t for t in chain[1:] if t in got]
            note = ", ".join(alt) if alt else ("" if ours == "yes"
                                               else "(nothing in CY%d)" % year)
            print(f"  {tk:<8}{metric:<10}{ours:<12}{note}")
    print("    'NO' with a named alternate is no longer a gap: the chain "
          "reads that tag\n    instead and source_url records which one. "
          "'NO' with nothing named is a\n    real gap -- no claim for "
          "that metric.\n    A frame covers ONE calendar year, so a filer "
          "absent here may still have a\n    series; the per-filer "
          "section below is what decides.")

    # THE SERIES, VIA companyconcept -- small payloads, and OUR annual
    # filter rather than a frame's "most closely fits".
    print(f"\n  ANNUAL SERIES for the first {limit} filers "
          f"(companyconcept, our own filtering, chain in order)")
    shown = 0
    lags: list = []
    stale_here: list = []
    for tk, _code, decision, _b in seed:
        cik = ciks.get(tk)
        if not cik or decision not in INGESTABLE or shown >= limit:
            continue        # consume rows are never written as claims
        shown += 1
        print(f"\n    {tk} — CIK {cik:010d}")
        for metric in TAGS_FOR_METRIC:
            s = pick_series(c, cik, metric, use_cache)
            print(f"      {metric}")
            for line in s["trail"]:
                print(f"        {line}")
            keep = s["facts"]
            if not keep:
                continue
            hi = keep[-1]
            print(f"        CHOSE {s['tag']}: {len(keep)} annual, "
                  f"{keep[0]['end']} .. {hi['end']}")
            print(f"        latest ${hi['val']:,.0f}  end {hi['end']}  "
                  f"filed {hi['filed']}  {hi['form']}")
            print(f"        source_url "
                  f"{CONCEPT.format(cik=cik, tag=s['tag'])}")
            if s["why"]:
                print(f"        rejected: {dict(s['why'])}")
            if (s["stale_days"] or 0) > STALE_SERIES_DAYS:
                stale_here.append(f"{tk}/{metric} ({s['stale_days']}d)")
            lags.append((_d(hi['filed']) - _d(hi['end'])).days)
    if lags:
        print(f"\n    AD-107, measured ONCE rather than printed per row: "
              f"across the {len(lags)}\n    series above a 10-K was filed "
              f"{min(lags)}-{max(lags)} days after the period end it "
              f"reports.\n    as_of_date is that period end, "
              f"published_date is the filing date, and\n    EDGAR is the "
              f"first source where the two genuinely differ.")
    else:
        print("\n    No series was found, so NO as_of/published lag was "
              "measured. The\n    AD-107 claim is untested by this run "
              "rather than confirmed (AD-116).")
    if stale_here:
        print(f"\n    STALE EVEN AFTER THE CHAIN: {', '.join(stale_here)}")
        print("    No tag in the chain had anything newer, so --apply "
              "would write these\n    with their real (old) as_of_date. "
              "That is correct, not a bug -- but it\n    is also the "
              "shape the chain was built to catch, so read the dates.")

    print("\n  NOT FILED, AND WHY")
    for m, why in NOT_FILED.items():
        print(f"    {m}: {why}")

    print(f"\n  requests used: {c.n}")
    print("\n  Nothing was written. Set each seed row's decision and "
          "org_code from the\n  identity table above, then --apply. Rows "
          "left 'pending' are refused.")
    return 0


INSERT = """
INSERT INTO cost_claims (
    subject_kind, subject_id, subject_label, metric, value, currency,
    as_of_date, published_date, method, derivation_basis,
    source, source_url, confidence, confidence_basis, organization_code)
VALUES (
    'enterprise', :subject_id, :subject_label, :metric, :value, 'USD',
    :as_of_date, :published_date, 'published', NULL,
    'sec_edgar', :source_url, NULL, NULL, :organization_code)
ON CONFLICT (subject_kind, subject_id, metric, source, as_of_date)
DO UPDATE SET
    value          = EXCLUDED.value,
    subject_label  = EXCLUDED.subject_label,
    published_date = EXCLUDED.published_date,
    organization_code = COALESCE(EXCLUDED.organization_code,
                                 cost_claims.organization_code),
    last_seen_at   = NOW()
RETURNING (xmax = 0) AS inserted
"""


def apply(c: Client, use_cache: bool, dry_run: bool) -> int:
    seed = read_seed()
    pending = [t for t, _o, d_, _b in seed if d_ == "pending"]
    if pending:
        print("REFUSING TO WRITE.\n")
        print(f"These seed rows are still 'pending': {pending}")
        print("\nA 'pending' row means nobody has read the survey for it "
              "and chosen an\norg_code. Writing it would guess the one "
              "thing this source exists to\navoid guessing. Run --survey, "
              "edit data/seed/sec_ciks.tsv, re-run.")
        return 1

    with get_engine().begin() as conn:
        if conn.execute(text(
                "SELECT to_regclass('public.cost_claims') IS NULL")
        ).scalar_one():
            print("cost_claims does not exist — apply 021 first.")
            return 1
        known = {r[0] for r in conn.execute(text(
            "SELECT code FROM organizations"))}

    live = [(t, o, d_) for t, o, d_, _b in seed if d_ in ("match", "claim")]
    bad = [(t, o) for t, o, d_ in live
           if d_ == "match" and (not o or o == "-" or o not in known)]
    if bad:
        print("REFUSING TO WRITE.\n")
        print(f"These rows say 'match' but name no code in organizations: "
              f"{bad}")
        print("Use 'claim' if the filer is real and organizations has no "
              "row for it —\nAD-085 allows a claim with no link, and that "
              "is different from a typo.")
        return 1

    ciks = resolve_tickers(c, [t for t, _o, _d_ in live], use_cache)
    inserted = updated = 0
    #: (headline, trail) per filer/metric pair, so the count means pairs
    #: and the detail stays whole. A truncated diagnostic is one you
    #: have to run again.
    skipped: list = []
    #: Which tag each metric was actually read from, and how often.
    #: The chain makes source='sec_edgar' heterogeneous on purpose;
    #: this is the line that stops it being silent.
    used: Counter = Counter()
    stale: list = []

    with get_engine().begin() as conn:
        for tk, org_code, decision in live:
            cik = ciks.get(tk)
            if not cik:
                skipped.append((f"{tk}: not in EDGAR's ticker file", []))
                continue
            code = org_code if decision == "match" else None
            name = tk
            for metric in TAGS_FOR_METRIC:
                # companyconcept carries entityName, so no submissions
                # request is needed here at all -- 16 fewer calls.
                s = pick_series(c, cik, metric, use_cache)
                name = s["entity_name"] or name
                if not s["facts"]:
                    skipped.append((
                        f"{tk}/{metric}: no series in any of the "
                        f"{len(TAGS_FOR_METRIC[metric])} chain tags",
                        list(s["trail"])))
                    continue
                used[(metric, s["tag"])] += 1
                # The URL ACTUALLY READ, which names the concept. See
                # the module docstring: this is what makes a chain
                # honest rather than silently heterogeneous.
                src = CONCEPT.format(cik=cik, tag=s["tag"])
                if (s["stale_days"] or 0) > STALE_SERIES_DAYS:
                    stale.append(
                        f"{tk}/{metric}: newest fact in the whole chain is "
                        f"{s['stale_days']}d old ({s['tag']}, ends "
                        f"{s['facts'][-1]['end']})")
                for f in s["facts"]:
                    if dry_run:
                        inserted += 1
                        continue
                    was_new = conn.execute(text(INSERT), {
                        # The CIK is the subject id. Zero-padded so it
                        # sorts and joins as EDGAR writes it.
                        "subject_id": f"CIK{cik:010d}",
                        "subject_label": f"{name} ({tk})"[:300],
                        "metric": metric,
                        "value": float(f["val"]),
                        "as_of_date": _d(f["end"]),
                        "published_date": _d(f["filed"]) or _d(f["end"]),
                        "source_url": src,
                        "organization_code": code,
                    }).scalar_one()
                    inserted += was_new
                    updated += not was_new

    print(f"\n  filers written    : {len(live)}")
    print(f"  requests used     : {c.n}")
    print(f"  claims inserted   : {inserted}")
    print(f"  claims updated    : {updated}")
    print(f"  linked to an org  : "
          f"{sum(1 for _t, _o, d_ in live if d_ == 'match')} of {len(live)} "
          f"filers (the rest are 'claim' — real money, no org row)")
    if used:
        print("  tag actually read :")
        for (metric, tag), n in sorted(used.items()):
            print(f"      {metric:<10}{tag:<62}{n} filer(s)")
    else:
        print("  tag actually read : NONE — no metric produced a series "
              "for any filer. That is\n                      a result to "
              "read, not a clean run (AD-116).")
    if stale:
        print(f"  stale series      : {len(stale)} — written with their own "
              f"real as_of_date,\n                      which is correct, "
              f"but no chain tag had anything newer:")
        for line in stale:
            print(f"      {line}")
    if skipped:
        print(f"  skipped           : {len(skipped)} filer/metric pair(s), "
              f"each with the tags tried:")
        for head, trail in skipped:
            print(f"      {head}")
            for line in trail:
                print(f"          {line}")
    if dry_run:
        print("\n  --dry-run: nothing was written.")
    else:
        print("\n  Now: python -m src.funding.usaspending --verify")
        print("  cost_claim_conflicts can finally fire — two sources exist.")
    return 0


def frames_mode(c: Client, year: int, use_cache: bool) -> int:
    """Cross-check written claims against a frame for the same year.

    Frames never WRITE a claim (see "WHICH ENDPOINT"), but they are the
    cheapest possible second opinion: one request returns one fact per
    filer nationwide, so a value we wrote from companyconcept can be
    compared against what the frame assigned to the same calendar year.

    A DISAGREEMENT IS NOT A BUG. A frame picks the fact that "most
    closely fits" CY<year> within +/-30 days; our filter takes the 10-K
    annual fact for its own period end. For a filer whose fiscal year
    ends in January the two legitimately differ, and seeing that is the
    point -- it is the same claim about a different period, which is
    exactly the distinction `as_of_date` exists to carry.
    """
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT subject_id, subject_label, metric, value, as_of_date,
                   source_url
              FROM cost_claims
             WHERE source = 'sec_edgar'
               AND extract(year FROM as_of_date) = :y
             ORDER BY metric, value DESC
        """), {"y": year}).all()
    if not rows:
        print(f"  no sec_edgar claims with an as_of_date in {year}.")
        return 0

    # Group by the concept each claim RECORDS, not by one tag per
    # metric. A claim written from `Revenues` compared against the
    # RevenueFromContractWithCustomer... frame would read "absent" and
    # mean nothing at all (AD-113). source_url names the tag, so read
    # it off the row instead of recalling it (AD-112).
    by_tag: dict = {}
    for sid, label, m, val, as_of, url in rows:
        by_tag.setdefault((m, tag_from_url(url)), []).append(
            (sid, label, val, as_of))

    print(f"\n  OURS vs THE FRAME, CY{year}")
    for (metric, tag), claims in sorted(
            (kv for kv in by_tag.items() if kv[0][1]),
            key=lambda kv: kv[0]):
        try:
            fr, hit = cached(c, FRAMES.format(tag=tag, year=year),
                             f"frame_{tag}_{year}", use_cache)
        except RuntimeError as exc:
            print(f"    {metric} / {tag}: frame unavailable -- {exc}")
            continue
        by_cik = {int(d["cik"]): d for d in fr.get("data", [])}
        print(f"\n    {metric}  ({tag}, {len(by_cik)} filers in the frame,"
              f" {'cached' if hit else 'fetched'})")
        for sid, label, val, as_of in claims:
            cik = int(str(sid).replace("CIK", ""))
            d = by_cik.get(cik)
            if not d:
                print(f"      {str(label)[:34]:<36}ours ${float(val):,.0f}"
                      f"   frame: absent")
                continue
            same = abs(float(d["val"]) - float(val)) < 1.0
            mark = "same" if same else "DIFFERS"
            print(f"      {str(label)[:34]:<36}ours ${float(val):,.0f}"
                  f"  frame ${float(d['val']):,.0f}  [{mark}]")
            if not same:
                print(f"        ours end {as_of}   frame "
                      f"{d.get('start')}..{d.get('end')}")
    for (metric, _none), claims in [kv for kv in by_tag.items()
                                    if not kv[0][1]]:
        print(f"\n    {metric}  (source_url names no concept — "
              f"{len(claims)} claim(s) NOT compared)")
        print("      These rows predate 2026-10-19, when source_url began "
              "recording the\n      companyconcept URL actually read. "
              "Which tag they came from is not\n      knowable from the "
              "row, and comparing them against a guessed tag would\n"
              "      invent the answer. Re-run --apply to restamp them.")
    print("\n  A difference is a period difference, not an error. Read the "
          "dates.")
    print(f"  requests used: {c.n}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--survey", action="store_true",
                    help="Identity, SIC and which tags exist. Writes "
                         "nothing.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--facts-for", type=int, default=4, metavar="N",
                    help="How many filers to pull companyfacts for during "
                         "--survey (default 4). Each is one request.")
    ap.add_argument("--max-requests", type=int,
                    default=DEFAULT_MAX_REQUESTS)
    ap.add_argument("--frames", type=int, metavar="YEAR",
                    help="Cross-check written claims for YEAR against the "
                         "frames API. Reads, never writes.")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    if not (args.survey or args.apply or args.frames):
        ap.error("pass --survey (safe, writes nothing), --apply or "
                 "--frames YEAR")

    c = Client(max_requests=args.max_requests)
    try:
        if args.frames:
            return frames_mode(c, args.frames, not args.no_cache)
        if args.survey:
            return survey(c, not args.no_cache, args.facts_for)
        return apply(c, not args.no_cache, args.dry_run)
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
