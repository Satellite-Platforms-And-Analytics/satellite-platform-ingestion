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

ONE TAG PER METRIC, AND NO FALLING BACK
=======================================
Revenue is reported under several XBRL tags -- `Revenues`,
`SalesRevenueNet`, `RevenueFromContractWithCustomerExcludingAssessedTax`
and more. Falling back across them would mean `source='sec_edgar'` plus
`metric='revenue'` silently meaning different things for different
companies, and `cost_claims` has no column that could record which tag
a row came from.

So: exactly one tag per metric. A company reporting under a different
tag gets NO claim, and the survey counts them, which turns a silent
heterogeneous mix into visible coverage. Same decision as refusing
`Total Outlays` from USAspending, for the same reason.

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
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CONCEPT = ("https://data.sec.gov/api/xbrl/companyconcept/"
           "CIK{cik:010d}/us-gaap/{tag}.json")
FRAMES = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{year}.json"
#: Kept for reference only. Returns every concept a filer has ever
#: reported; companyconcept returns the two this module wants.
FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FILING_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}"

#: EDGAR publishes 10/s. We take half of it.
MIN_INTERVAL_S = 0.2
DEFAULT_MAX_REQUESTS = 60

SEED = (pathlib.Path(__file__).resolve().parent.parent.parent
        / "data" / "seed" / "sec_ciks.tsv")
CACHE_DIR = (pathlib.Path(__file__).resolve().parent.parent.parent
             / "data" / "cache" / "sec_edgar")
CACHE_TTL_S = 7 * 24 * 3600

#: Exactly one tag per metric. See the docstring.
TAG_FOR_METRIC = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "rd_spend": "ResearchAndDevelopmentExpense",
}

#: Other tags the same money is sometimes reported under. NOT read --
#: counted by the survey, so "no claim" is distinguishable from
#: "reported somewhere we are not looking".
ALSO_SEEN = {
    "revenue": ("Revenues", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax"),
    "rd_spend": ("ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",),
}

NOT_FILED = {
    "market_cap": "EDGAR has shares outstanding and no price. Market cap "
                  "would be derived from a second source and needs a "
                  "derivation_basis naming the price and its date.",
}

#: SIC codes that say "this filer might be in scope", taken VERBATIM from
#: the SEC's own Division of Corporation Finance SIC list (read at source
#: 2026-10-12), which is the list EDGAR itself assigns from. Reported,
#: never used to filter: a filer in the seed is there because a person put
#: it there, and the SIC only ever adds a line to a report.
#:
#: Written from 2026-10-12 onward because the first version of this
#: constant carried "3761" and "3669", neither of which exists in the
#: SEC's list. The real code for the obvious one is 3760. A discovery
#: query filtered on 3761 would have returned zero rows and looked like
#: an empty universe rather than a typo -- the same failure shape as
#: AD-105, where an instrument reported its own setting.
SPACE_SIC = {
    "3760": "Guided missiles & space vehicles & parts",
    "3720": "Aircraft & parts",
    "3721": "Aircraft",
    "3724": "Aircraft engines & engine parts",
    "3728": "Aircraft parts & auxiliary equipment, NEC",
    "3812": "Search, detection, navigation, guidance, aeronautical systems",
    "3663": "Radio & TV broadcasting & communications equipment",
    "4812": "Radiotelephone communications",
    "4899": "Communications services, NEC",
}


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
            raise RuntimeError(
                f"HTTP {e.code} from {url}\n"
                f"{e.read().decode('utf-8', 'replace')[:300]}") from None


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
        if decision not in ("pending", "match", "claim", "refuse"):
            raise SystemExit(f"seed row {tk}: bad decision {decision!r}")
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
    for metric, tag in TAG_FOR_METRIC.items():
        for t in (tag,) + tuple(ALSO_SEEN.get(metric, ())):
            try:
                fr, _h = cached(c, FRAMES.format(tag=t, year=year),
                                f"frame_{t}_{year}", use_cache)
            except RuntimeError as exc:
                print(f"    {t}: frame unavailable ({str(exc)[:60]})")
                continue
            here = {int(d["cik"]) for d in fr.get("data", [])}
            for cik in here & set(cik_to_tk):
                reports.setdefault(cik_to_tk[cik], set()).add(t)
            print(f"    {t:<62}{len(fr.get('data', [])):>6} filers "
                  f"nationwide, {len(here & set(cik_to_tk))} of ours")

    print(f"\n  {'ticker':<8}{'metric':<10}{'our tag?':<10}reports instead")
    for tk, _code, decision, _b in seed:
        if decision == "refuse" or not ciks.get(tk):
            continue
        for metric, tag in TAG_FOR_METRIC.items():
            got = reports.get(tk, set())
            ours = "yes" if tag in got else "NO"
            alt = sorted(got & set(ALSO_SEEN.get(metric, ())))
            note = ", ".join(alt) if alt else ("" if ours == "yes"
                                               else "(nothing in CY%d)" % year)
            print(f"  {tk:<8}{metric:<10}{ours:<10}{note}")
    print("    A filer reporting only an alternate tag gets NO claim for "
          "that metric.\n    One tag per metric, deliberately -- and now "
          "the gap is counted rather than silent.")

    # THE SERIES, VIA companyconcept -- small payloads, and OUR annual
    # filter rather than a frame's "most closely fits".
    print(f"\n  ANNUAL SERIES for the first {limit} filers "
          f"(companyconcept, our own filtering)")
    shown = 0
    for tk, _code, decision, _b in seed:
        cik = ciks.get(tk)
        if not cik or decision == "refuse" or shown >= limit:
            continue
        shown += 1
        print(f"\n    {tk} — CIK {cik:010d}")
        for metric, tag in TAG_FOR_METRIC.items():
            try:
                cc, _h = cached(c, CONCEPT.format(cik=cik, tag=tag),
                                f"cc_{cik:010d}_{tag}", use_cache)
            except RuntimeError as exc:
                print(f"      {metric:<10}no companyconcept "
                      f"({str(exc)[:50]})")
                continue
            keep, why = annual_facts(cc, tag)
            if keep:
                lo, hi = keep[0], keep[-1]
                print(f"      {metric:<10}{len(keep)} annual, "
                      f"{lo['end']} .. {hi['end']}")
                print(f"        latest ${hi['val']:,.0f}  end {hi['end']}  "
                      f"filed {hi['filed']}  {hi['form']}")
                lag = (_d(hi['filed']) - _d(hi['end'])).days
                print(f"        as_of != published by {lag} days — the "
                      f"first source where AD-107's two dates differ")
            else:
                print(f"      {metric:<10}no usable annual fact")
            if why:
                print(f"        rejected: {dict(why)}")

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
    skipped: list = []

    with get_engine().begin() as conn:
        for tk, org_code, decision in live:
            cik = ciks.get(tk)
            if not cik:
                skipped.append(f"{tk}: not in EDGAR's ticker file")
                continue
            code = org_code if decision == "match" else None
            name = tk
            for metric, tag in TAG_FOR_METRIC.items():
                # companyconcept carries entityName, so no submissions
                # request is needed here at all -- 16 fewer calls.
                try:
                    cc, _h = cached(c, CONCEPT.format(cik=cik, tag=tag),
                                    f"cc_{cik:010d}_{tag}", use_cache)
                except RuntimeError as exc:
                    skipped.append(f"{tk}/{metric}: {str(exc)[:50]}")
                    continue
                name = cc.get("entityName") or name
                keep, _why = annual_facts(cc, tag)
                if not keep:
                    skipped.append(f"{tk}/{metric}: no usable annual fact")
                    continue
                for f in keep:
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
                        "source_url": FILING_URL.format(cik=cik),
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
    if skipped:
        print(f"  skipped           : {len(skipped)}")
        for s in skipped[:10]:
            print(f"      {s}")
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
            SELECT subject_id, subject_label, metric, value, as_of_date
              FROM cost_claims
             WHERE source = 'sec_edgar'
               AND extract(year FROM as_of_date) = :y
             ORDER BY metric, value DESC
        """), {"y": year}).all()
    if not rows:
        print(f"  no sec_edgar claims with an as_of_date in {year}.")
        return 0

    print(f"\n  OURS vs THE FRAME, CY{year}")
    for metric, tag in TAG_FOR_METRIC.items():
        try:
            fr, hit = cached(c, FRAMES.format(tag=tag, year=year),
                             f"frame_{tag}_{year}", use_cache)
        except RuntimeError as exc:
            print(f"    {metric}: frame unavailable ({str(exc)[:60]})")
            continue
        by_cik = {int(d["cik"]): d for d in fr.get("data", [])}
        print(f"\n    {metric}  ({tag}, {len(by_cik)} filers in the frame,"
              f" {'cached' if hit else 'fetched'})")
        for sid, label, m, val, as_of in rows:
            if m != metric:
                continue
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
