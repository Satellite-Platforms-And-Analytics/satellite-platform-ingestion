"""
USAspending.gov federal awards -> `cost_claims`.

    python -m src.funding.usaspending --survey
    python -m src.funding.usaspending --apply --limit 500

First ingester for the money layer. `cost_claims` has been live and
EMPTY since 2026-10-06, and a schema with no rows has proved only
itself.

RATE LIMITS (AD-060, docs/API_USAGE_POLICY.md)
==============================================
**USAspending publishes NO rate limit.** Not in the API docs, not in the
`fedspendingtransparency/usaspending-api` README. The 10-05 survey found
a third-party guide reporting 20 concurrent workers without a 429, and
recorded that as *evidence about that author's afternoon, not about the
terms*.

AD-060 is the rule for this exact situation: **an unpublished ceiling is
found by hitting it**, and the standing instruction on this project is
not to risk a hold on any account. So the ceiling here is ours, not
theirs:

  - one request at a time, never concurrent
  - MIN_INTERVAL_S between requests, enforced in Client.post
  - a hard --max-requests cap, so a bug cannot spiral into a thousand
    calls while nobody is watching
  - a disk cache, because on 09-14 this project re-downloaded 13,517
    `gp_history` records it already held and settled that **"we were
    allowed to" is not the standard**

WHY --survey EXISTS AND WHY IT IS NOT OPTIONAL
==============================================
This module was written on 2026-10-12 without ever having seen a
response: `api.usaspending.gov` is not reachable from either environment
Claude can run code in, so every field name below is a GUESS from
documentation, and the one that matters most is genuinely ambiguous.

`spending_by_award` returns a money column called "Award Amount". For a
contract that is the **currently obligated** total — but the
base-and-all-options ceiling is a different and much larger number, and
`021` has separate metrics for them:

    contract_obligated   what has actually been put on the contract
    contract_potential   what it could reach if every option is taken

**Filing a potential value as an obligation would be the costliest kind
of wrong number here** — right magnitude, right recipient, wrong
meaning, and nothing downstream would catch it. So `--apply` refuses to
run unless the field it is about to read was actually present in the
response, and `--survey` prints every money-ish column side by side so a
human decides which metric each one is.

That is the same shape as AD-062: predict, then measure, and write the
prediction down first so the measurement cannot be read backwards.

PREDICTED MATCH RATE, RECORDED BEFORE MATCHING (AD-062)
=======================================================
**3-6% strict** for recipients -> `organizations`. GCAT names operators
and agencies; USAspending names thousands of primes and subs, most of
which have never operated a spacecraft. A low overlap is the expected
result, not a defect (AD-083).

**A failed match never drops a claim.** `organization_code` is an
attribute, not the spine (AD-085), and it is nullable precisely so that
money about a company we do not know is still money we recorded.

**Only strict and curated-alias matches are written.** `match_org`
returns `(code, how)`, and 015 requires a code and its match_method to
be written together, because a code with no method makes a loosened
match indistinguishable from a strict one. `cost_claims` has no
match_method column, so a relaxed match has nowhere to declare itself --
and a prefix-stripped match on federal recipient names is where "Boeing"
and "Boeing Defense, Space & Security" become one row. Relaxed
candidates are counted and reported; they are not written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
from datetime import date, datetime

from src.env import bootstrap

bootstrap()

from sqlalchemy import text                                  # noqa: E402

from src.catalog.org_match import (                          # noqa: E402
    load_org_index, match_org,
)
from src.db.writer import get_engine                         # noqa: E402
from src.useragent import user_agent                         # noqa: E402

BASE = "https://api.usaspending.gov/api/v2"
SEARCH = f"{BASE}/search/spending_by_award/"
AWARD_URL = "https://www.usaspending.gov/award/"

#: Self-imposed, because nothing is published. See RATE LIMITS above.
MIN_INTERVAL_S = 1.0
DEFAULT_MAX_REQUESTS = 40

CACHE_DIR = (pathlib.Path(__file__).resolve().parent.parent.parent
             / "data" / "cache" / "usaspending")
CACHE_TTL_S = 7 * 24 * 3600

#: Guided missile and space vehicle manufacturing, and its parts.
NAICS = ["336414", "336415", "336419"]

#: Contract award types. Grants and loans have different money semantics
#: and are deliberately out of scope for the first pass.
AWARD_TYPES = ["A", "B", "C", "D"]

#: Columns we ask for. Names are from the documentation and are UNVERIFIED
#: -- --survey reports which of these actually came back.
FIELDS = [
    "Award ID", "Recipient Name", "Award Amount", "Total Outlays",
    "Awarding Agency", "Awarding Sub Agency", "Start Date", "End Date",
    "Last Modified Date", "Description", "NAICS", "recipient_id",
    "generated_internal_id",
]

#: Which response column means which `cost_claims.metric`.
#:
#: SETTLED BY THE 2026-10-12 SURVEY, not by documentation.
#:
#: "Award Amount" -> contract_obligated. Two pieces of evidence, not one:
#: the base-and-all-options ceiling was neither requested nor returned,
#: so there is no second column it could be confused with; and Award
#: Amount exceeded Total Outlays on every row that had both, which is
#: the right direction for obligated-vs-disbursed and the wrong
#: direction for anything else.
METRIC_FOR_FIELD = {
    "Award Amount": "contract_obligated",
}

#: "Total Outlays" IS RETURNED AND IS DELIBERATELY NOT FILED.
#:
#: Two independent reasons, either sufficient:
#:
#: 1. `021`'s metric CHECK has no outlay metric. contract_obligated is
#:    money committed; an outlay is money disbursed. Filing one as the
#:    other is the error this module was built to avoid.
#: 2. The series is known-incomplete and the incompleteness is invisible
#:    in the value. Federal outlay reporting (DATA Act File C) only
#:    becomes complete from FY2017, which is why the survey's ISS row
#:    shows $2.71B outlayed against $22.65B obligated on a contract
#:    running since 1993, and why three of five sampled rows showed
#:    0.0 or None. A reader cannot tell "nothing was spent" from "nobody
#:    reported it", and `cost_claims` has no field that would say so --
#:    `derivation_basis` is for derived and inferred claims, and an
#:    outlay figure is published, not derived.
#:
#: Recorded as a decision rather than dropped, for the same reason the
#: Data Source Register stores the CSIS rejection: otherwise somebody
#: helpfully adds it.
NOT_FILED = {
    "Total Outlays": "no outlay metric in 021; coverage starts FY2017 and "
                     "the gap is invisible in the number",
}

#: Anything money-ish, so the survey can show them together and a human
#: can see whether two of them differ by an order of magnitude.
MONEY_HINTS = ("amount", "obligat", "outlay", "value", "ceiling", "total")


class Client:
    """One request at a time, throttled, counted, capped."""

    def __init__(self, max_requests: int = DEFAULT_MAX_REQUESTS):
        self.n = 0
        self.max_requests = max_requests
        self._last = 0.0
        self.ua = user_agent("federal award research",
                             rate="self-limited to 1 req/s; no published limit")

    def post(self, url: str, body: dict) -> dict:
        if self.n >= self.max_requests:
            raise RuntimeError(
                f"request cap reached ({self.max_requests}). Raise it with "
                "--max-requests if that is really what you want; it exists "
                "so a bug cannot spend a thousand calls unattended.")
        wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)

        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": self.ua},
            method="POST")
        self.n += 1
        self._last = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise RuntimeError(f"HTTP {e.code} from {url}\n{detail}") from None


def build_body(page: int, limit: int, since: str, until: str,
               naics: "list[str]", order: str = "desc") -> dict:
    """`order` is a parameter because the default makes the sample lie.

    Sorted descending, page 1 is THE LARGEST FEDERAL SPACE CONTRACTS IN
    THE COUNTRY -- Boeing, Lockheed, Northrop, the primes any space
    catalogue already knows by name. The 2026-10-12 dry run matched
    21/25 recipients (84%) against a prediction of 3-6%, and the obvious
    readings were both wrong: the rule is not loose and the data is not
    better than expected. **The sample was not the population.**

    This is the 09-14 TechPort defect in mirror image. There, a survey
    that measured its own sampling read as a fivefold improvement on
    identical data. Here, sorting by money size read as a fourteenfold
    one. Same error, opposite sign.

    So `--order asc` exists to be run: the same query over the SMALLEST
    awards samples the subcontractor tail, and if the match rate there
    collapses toward the prediction, sample bias is confirmed and the
    matcher is exonerated. If it stays near 84%, the matcher is wrong and
    the prediction was right.
    """
    return {
        "filters": {
            "award_type_codes": AWARD_TYPES,
            "naics_codes": naics,
            "time_period": [{"start_date": since, "end_date": until}],
        },
        "fields": FIELDS,
        "sort": "Award Amount",
        "order": order,
        "limit": limit,
        "page": page,
    }


def fetch_page(c: Client, body: dict, use_cache: bool = True):
    """Returns (payload, from_cache). Cached by a hash of the request."""
    key = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    f = CACHE_DIR / f"{key}.json"
    if use_cache and f.is_file():
        if time.time() - f.stat().st_mtime < CACHE_TTL_S:
            try:
                return json.loads(f.read_text(encoding="utf-8")), True
            except Exception:                                # noqa: BLE001
                pass                      # unreadable cache is not an error
    payload = c.post(SEARCH, body)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass                              # a cache we cannot write is not
    return payload, False                 # a reason to fail


def _date(v) -> "date | None":
    if not v:
        return None
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def survey(c: Client, limit: int, since: str, until: str,
           naics: "list[str]", use_cache: bool, order: str) -> int:
    """Report what the API actually returns. Writes nothing, anywhere."""
    print("\nPREDICTIONS, recorded before any measurement (AD-062)")
    print("  recipients -> organizations, strict:  3-6%")
    print("  a low overlap is the expected result, not a defect (AD-083)\n")

    body = build_body(1, limit, since, until, naics, order)
    payload, hit = fetch_page(c, body, use_cache)
    results = payload.get("results") or []
    meta = payload.get("page_metadata") or {}

    print(f"requests used: {c.n}   from cache: {int(hit)}")
    print(f"rows returned: {len(results)}   "
          f"page_metadata: {json.dumps(meta)[:120]}")
    if not results:
        print("\nNo rows. Either the filters match nothing or the request "
              "shape is wrong — the payload is printed below.")
        print(json.dumps(payload)[:1200])
        return 1

    got = set()
    for r in results:
        got |= set(r.keys())
    asked = set(FIELDS)
    print(f"\nfields ASKED FOR and returned   ({len(asked & got)}/{len(asked)}):")
    for k in sorted(asked & got):
        print(f"    {k}")
    if asked - got:
        print("\nfields ASKED FOR and MISSING — every one of these is a "
              "guess that was wrong:")
        for k in sorted(asked - got):
            print(f"    {k}")
    if got - asked:
        print("\nfields returned that we did NOT ask for:")
        for k in sorted(got - asked):
            print(f"    {k}")

    money = [k for k in sorted(got)
             if any(h in k.lower() for h in MONEY_HINTS)]
    print("\nMONEY COLUMNS, SIDE BY SIDE — this is the decision to make.")
    print("Two columns differing by an order of magnitude is obligated vs")
    print("base-and-all-options. Filing one as the other is the costliest")
    print("wrong number available here.\n")
    print("    " + " | ".join(f"{k[:24]}" for k in money))
    for r in results[:5]:
        print("    " + " | ".join(f"{str(r.get(k))[:24]}" for k in money))

    print("\nmetric mapping this module would use on --apply:")
    for f_, m in METRIC_FOR_FIELD.items():
        state = "PRESENT" if f_ in got else "*** ABSENT — apply would refuse"
        print(f"    {f_!r} -> {m}    [{state}]")
    print("\nreturned, and deliberately NOT filed:")
    for f_, why in NOT_FILED.items():
        here = "present" if f_ in got else "not returned"
        print(f"    {f_!r} ({here})\n        {why}")

    print("\nfirst row in full, so the date semantics can be checked:")
    print(json.dumps(results[0], indent=2)[:1400])

    print("\nNothing was written. Decide the metric mapping, then --apply.")
    return 0


INSERT = """
INSERT INTO cost_claims (
    subject_kind, subject_id, subject_label, metric, value, currency,
    as_of_date, published_date, method, derivation_basis,
    source, source_url, confidence, confidence_basis, organization_code)
VALUES (
    'contract', :subject_id, :subject_label, :metric, :value, 'USD',
    :as_of_date, :published_date, 'published', NULL,
    'usaspending', :source_url, NULL, NULL, :organization_code)
ON CONFLICT (subject_kind, subject_id, metric, source, as_of_date)
DO UPDATE SET
    value         = EXCLUDED.value,
    subject_label = EXCLUDED.subject_label,
    published_date = EXCLUDED.published_date,
    source_url    = EXCLUDED.source_url,
    -- Never erase a match an earlier pass made because this pass failed
    -- to match. A blank is absence of evidence, not evidence of absence.
    organization_code = COALESCE(EXCLUDED.organization_code,
                                 cost_claims.organization_code),
    last_seen_at  = NOW()
RETURNING (xmax = 0) AS inserted
"""


def apply(c: Client, limit: int, pages: int, since: str, until: str,
          naics: "list[str]", use_cache: bool, dry_run: bool,
          order: str) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        if conn.execute(text(
                "SELECT to_regclass('public.cost_claims') IS NULL")
        ).scalar_one():
            print("cost_claims does not exist — apply 021 first.")
            return 1
        strict, relaxed = load_org_index(conn, report=True)
        # Codes that sit BELOW something else in the corporate tree.
        # Independent of the matcher's preference order, on purpose: the
        # matcher's job is "which row does this name mean", and this is
        # "may a contract recipient be attributed to a child entity".
        # Two controls, because on 2026-10-12 the matcher's answer for
        # "THE BOEING COMPANY" was a subsidiary and the value was right,
        # which is the case a single control cannot catch.
        subsidiaries = {r[0] for r in conn.execute(text(
            "SELECT code FROM organizations WHERE parent_code IS NOT NULL"))}

    rows, from_cache = [], 0
    for page in range(1, pages + 1):
        body = build_body(page, limit, since, until, naics, order)
        payload, hit = fetch_page(c, body, use_cache)
        from_cache += hit
        got = payload.get("results") or []
        rows.extend(got)
        if len(got) < limit:
            break

    if not rows:
        print("No rows returned. Run --survey before assuming why.")
        return 1

    present = set().union(*(set(r.keys()) for r in rows))
    missing = [f for f in METRIC_FOR_FIELD if f not in present]
    if missing:
        print("REFUSING TO WRITE.\n")
        print(f"These columns drive the metric mapping and are not in the "
              f"response: {missing}")
        print("Writing a claim whose metric was inferred from an absent "
              "column is how a potential value becomes an obligation.")
        print("Run --survey, read the money columns, fix METRIC_FOR_FIELD.")
        return 1

    inserted = updated = matched = relaxed_declined = 0
    subsidiary_declined = 0
    #: (recipient, code, how). A match rate is a summary statistic and
    #: cannot distinguish 21 correct matches from 21 wrong ones. The
    #: pairs can.
    seen: "list[tuple]" = []
    skipped: "list[str]" = []
    today = date.today()

    with engine.begin() as conn:
        for r in rows:
            recipient = r.get("Recipient Name")
            piid = r.get("Award ID")
            # `Award ID` is the PIID, which is unique PER AGENCY, not
            # globally -- two agencies can issue the same PIID. The
            # natural key would then collide and one agency's award
            # would overwrite the other's, with no error.
            # `generated_internal_id` is USAspending's own unique award
            # key ("CONT_AWD_NAS1510000_8000_-NONE-_-NONE-") and is what
            # the award's public URL is built from.
            award_id = r.get("generated_internal_id") or piid
            if not award_id:
                skipped.append("no award id")
                continue

            # THE VINTAGE. Settled by the 10-12 survey, against my own
            # first guess, which was wrong in the costliest available way.
            #
            # I had as_of = Start Date. The survey's top row is the ISS
            # contract: Start Date 1993-11-15, Award Amount
            # $22,653,589,199.84, Last Modified 2026-10-05. That figure
            # is the CUMULATIVE OBLIGATED TOTAL AS OF TODAY, not a 1993
            # number. Filing it with as_of = 1993-11-15 would have
            # labelled a 2026 total as a 1993 figure -- precisely what
            # AD-100 forbids: "$50M in 2015 USD is not $50M in 2026 USD."
            # Thirty-three years of inflation, silently mis-stamped, on
            # the largest claim in the set.
            #
            # So the vintage of a cumulative total is the date the total
            # is current as of: Last Modified Date.
            #
            # That choice pays a second time through the natural key
            # (subject_kind, subject_id, metric, source, as_of_date).
            # With Start Date, the key never changes, so next month's
            # larger obligation would UPDATE this row and the old figure
            # would be gone. With Last Modified, a changed total arrives
            # as a NEW row -- the obligation's history accumulates by
            # itself, and the earlier claim survives, which is the whole
            # point of AD-077.
            #
            # Period of performance (Start/End Date) is context about the
            # contract, not the vintage of the money. 021 has nowhere to
            # put it and does not pretend otherwise.
            as_of = _date(r.get("Last Modified Date"))
            if as_of is None:
                as_of = today         # no vintage stated: today is honest
            published = as_of         # the source stated it when it stated it

            # match_org returns (code, how). Its own docstring records
            # that the first version took [0] and discarded `how`,
            # "which is how a loosening becomes invisible" -- and 015
            # requires code and match_method to be written together.
            #
            # `cost_claims` has no match_method column, so there is
            # nowhere to put `how`. The answer is NOT to drop it and
            # write the code anyway: it is to write a code only when the
            # match needs no qualifier. A relaxed match strips agency
            # prefixes, and on federal recipient names that is exactly
            # where "Boeing", "The Boeing Company" and "Boeing Defense,
            # Space & Security" collapse into one row -- three legal
            # entities with three different sets of money.
            #
            # Relaxed candidates are counted and reported, not silently
            # discarded, so the cost of this choice stays visible.
            code, how = match_org(recipient, strict, relaxed)
            if code in subsidiaries:
                # A federal award names the contracting legal entity. If
                # the name resolved to a row with a parent, the level is
                # wrong even when the money is right (AD-101), and no
                # column on cost_claims could record the discrepancy.
                # Withhold the code; keep the claim.
                subsidiary_declined += 1
                code, how = None, "subsidiary-declined"
            seen.append((recipient, code, how))
            if how in ("strict", "alias"):
                matched += 1
            else:
                if code:
                    relaxed_declined += 1
                code = None

            for field, metric in METRIC_FOR_FIELD.items():
                raw = r.get(field)
                if raw in (None, ""):
                    skipped.append(f"{award_id}: {field} empty")
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    skipped.append(f"{award_id}: {field}={raw!r} not a number")
                    continue
                if dry_run:
                    inserted += 1
                    continue
                was_new = conn.execute(text(INSERT), {
                    "subject_id": str(award_id),
                    # subject_label is the ONLY free text on the row, and
                    # organization_code is NULL whenever the match fails
                    # -- which is most of the time by design (AD-085).
                    # If the recipient name is not here, an unmatched
                    # claim records money with no record of who received
                    # it, and nothing could re-match it later. So the
                    # label carries the recipient first, then what the
                    # source called the work.
                    "subject_label": " — ".join(
                        x for x in (recipient, r.get("Description"),
                                    piid) if x)[:300]
                    or str(award_id),
                    "metric": metric,
                    "value": value,
                    "as_of_date": as_of,
                    "published_date": published,
                    "source_url": f"{AWARD_URL}{award_id}",
                    "organization_code": code,
                }).scalar_one()
                inserted += was_new
                updated += not was_new

    print(f"\n  rows fetched      : {len(rows)}")
    print(f"  requests used     : {c.n}   from cache: {from_cache}")
    print(f"  claims inserted   : {inserted}")
    print(f"  claims updated    : {updated}")
    # PER ROW IS THE WRONG DENOMINATOR, and it flattered the matcher by
    # 34 points on 2026-10-12. The 25 descending rows held about six
    # distinct companies -- Lockheed Martin fourteen times, Raytheon four
    # -- so a per-row rate measures how many contracts one company
    # happens to hold, not how well names resolve. Reported first, and
    # second, because the distinct figure is the one that means anything.
    names = {str(n).strip().upper() for n, _c, _h in seen if n}
    hit = {str(n).strip().upper() for n, c, h in seen
           if n and h in ("strict", "alias")}
    d_pct = 100.0 * len(hit) / len(names) if names else 0.0
    pct = 100.0 * matched / len(rows)
    print(f"  recipients matched: {len(hit)}/{len(names)} DISTINCT "
          f"({d_pct:.1f}%)   —  {matched}/{len(rows)} rows ({pct:.1f}%)")
    print(f"      predicted 3-6% strict, on distinct names. In band: "
          f"{'yes' if 3 <= d_pct <= 6 else 'NO — read the next line'}")
    print("      Much higher than predicted usually means the match rule "
          "is too loose, not that the data is better than expected "
          "(AD-062) --")
    print("      BUT with --order desc it more likely means the SAMPLE is "
          "not the population:")
    print("      the largest space contracts go to the few primes any "
          "catalogue knows. Run")
    print("      --order asc to sample the subcontractor tail before "
          "believing either reading.")
    if subsidiary_declined:
        print(f"  subsidiary DECLINED: {subsidiary_declined} — the name "
              "resolved to a row with a parent_code, so the claim is "
              "written with no code rather than attributed one level down")
    if relaxed_declined:
        print(f"  relaxed, DECLINED : {relaxed_declined} — a prefix-stripped "
              "name matched an org, and was not written")
        print("      cost_claims has no match_method column, so a loosened "
              "match could not be labelled as one. Writing it anyway is "
              "the defect org_match.py's own docstring records.")
    if skipped:
        print(f"  skipped           : {len(skipped)}")
        for s in skipped[:8]:
            print(f"      {s}")
    if dry_run:
        print("\n  EVERY RECIPIENT AND WHAT IT MATCHED")
        print("  A rate cannot tell 21 right matches from 21 wrong ones.")
        print(f"  {'recipient':<44}{'how':<10}code")
        for name, code, how in seen[:40]:
            print(f"  {str(name)[:42]:<44}{str(how or '-'):<10}"
                  f"{code or '-'}")
        print("\n  --dry-run: nothing was written.")
    return 0


def verify() -> int:
    """What is actually in `cost_claims`, and do the views work on it?

    021's two views have never had a row to resolve. `cost_claims_resolved`
    is a DISTINCT ON over an ordering, and `cost_claim_conflicts` fires on
    HAVING count(DISTINCT source) > 1 AND min <> max -- neither can be
    wrong in an interesting way against an empty table, so neither has
    been tested against anything but the 25 synthetic rows in
    tests/test_cost_claims.py, every one of which was rolled back.

    Read-only.
    """
    engine = get_engine()
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM cost_claims")).scalar_one()
        print(f"\n  cost_claims rows                 {n}")
        if not n:
            print("  Nothing to verify. Run --apply first.")
            return 0

        print("\n  BY SOURCE, METRIC AND HIERARCHY")
        for row in conn.execute(text("""
            SELECT source, metric, hierarchy, count(*) AS n,
                   min(as_of_date) AS lo, max(as_of_date) AS hi,
                   sum(value) AS total, count(organization_code) AS coded
              FROM cost_claims
             GROUP BY 1, 2, 3 ORDER BY 4 DESC
        """)):
            print(f"    {row.source}/{row.metric}/{row.hierarchy}: "
                  f"{row.n} rows, as_of {row.lo}..{row.hi}, "
                  f"coded {row.coded}/{row.n}")
            # A sum is printed ONCE, here, and labelled -- AD-102: a
            # roll-up is an inference carrying coverage, and this one's
            # coverage is "whatever the last sample happened to fetch".
            print(f"      sum ${row.total:,.0f} — a LOWER BOUND over this "
                  f"sample only, not a market figure (AD-102)")

        print("\n  is_forecast, which 021 derives rather than accepts")
        for row in conn.execute(text(
                "SELECT is_forecast, count(*) FROM cost_claims "
                "GROUP BY 1 ORDER BY 1")):
            print(f"    {row[0]!s:<6}{row[1]}")

        print("\n  cost_claims_resolved — one row per subject+metric")
        res = conn.execute(text(
            "SELECT count(*) FROM cost_claims_resolved")).scalar_one()
        subj = conn.execute(text(
            "SELECT count(DISTINCT (subject_kind, subject_id, metric)) "
            "FROM cost_claims")).scalar_one()
        ok = "ok" if res == subj else "*** MISMATCH"
        print(f"    {res} resolved rows for {subj} subject+metric keys  [{ok}]")

        print("\n  cost_claim_conflicts — disagreement as a surface")
        rows = conn.execute(text(
            "SELECT subject_label, metric, low, high, spread_ratio "
            "FROM cost_claim_conflicts ORDER BY spread DESC LIMIT 10")).all()
        if not rows:
            print("    none — expected with ONE source. A conflict needs "
                  "two sources disagreeing,\n    so this stays empty until "
                  "SEC EDGAR lands. Empty here is the view working,\n"
                  "    not the view untested: the agreement case is what "
                  "tests/test_cost_claims.py\n    asserts must NOT fire.")
        for r in rows:
            print(f"    {str(r.subject_label)[:40]:<42}{r.metric:<20}"
                  f"{r.low} .. {r.high}  x{r.spread_ratio}")

        print("\n  three rows in full")
        for r in conn.execute(text("""
            SELECT subject_id, left(subject_label, 54) AS label, value,
                   as_of_date, published_date, method, organization_code
              FROM cost_claims ORDER BY value DESC LIMIT 3
        """)):
            print(f"    {r.label}")
            print(f"      {r.subject_id}")
            print(f"      ${r.value:,.2f}  as_of {r.as_of_date}  "
                  f"published {r.published_date}  {r.method}  "
                  f"org={r.organization_code or '-'}")
    print("\n  Read-only. Nothing was changed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--survey", action="store_true",
                    help="Report what the API returns. Writes nothing.")
    ap.add_argument("--verify", action="store_true",
                    help="Read back what is in cost_claims and exercise "
                         "both views. Writes nothing, fetches nothing.")
    ap.add_argument("--apply", action="store_true",
                    help="Write claims into cost_claims.")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --apply: map and count, write nothing.")
    ap.add_argument("--limit", type=int, default=25,
                    help="Rows per page (API max 100). Default 25.")
    ap.add_argument("--pages", type=int, default=1,
                    help="Pages to walk. The paginated search degrades "
                         "past offset 5,000; a real backfill wants the "
                         "bulk download endpoint, not this.")
    ap.add_argument("--since", default="2024-10-01")
    ap.add_argument("--until", default=date.today().isoformat())
    ap.add_argument("--naics", default=",".join(NAICS))
    ap.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    ap.add_argument("--order", choices=("desc", "asc"), default="desc",
                    help="desc samples the largest awards, which is a "
                         "sample biased toward names a space catalogue "
                         "knows. asc samples the subcontractor tail. See "
                         "build_body.")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    if args.verify:
        return verify()
    if not (args.survey or args.apply):
        ap.error("pass --survey, --apply or --verify")
    if args.limit > 100:
        ap.error("the API caps limit at 100 per page")

    c = Client(max_requests=args.max_requests)
    naics = [s.strip() for s in args.naics.split(",") if s.strip()]
    use_cache = not args.no_cache

    try:
        if args.survey:
            return survey(c, args.limit, args.since, args.until,
                          naics, use_cache, args.order)
        return apply(c, args.limit, args.pages, args.since, args.until,
                     naics, use_cache, args.dry_run, args.order)
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
