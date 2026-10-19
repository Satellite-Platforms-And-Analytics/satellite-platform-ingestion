"""
The api.data.gov rules, as assertions.

docs/API_USAGE_POLICY.md records what the developer manual says. A
document records; it does not enforce. These are the clauses that can be
undone by an ordinary refactor without anyone noticing, so they are
tested rather than written down:

  - the key travels in a header, not the query string
  - the client stops short of the limit instead of discovering it
  - the rate limit is read from the server, never asserted from memory
  - the 403 branch keys on documented error codes

Offline. No request is made; only the request the client would make.
"""
from __future__ import annotations

import pathlib

import importlib

import pytest

techport = importlib.import_module("check_techport")

KEY = "k" * 40


@pytest.fixture()
def client() -> "techport.Client":
    return techport.Client(KEY)


def test_key_travels_in_a_header(client) -> None:
    assert client.session.headers.get("X-Api-Key") == KEY


def test_key_is_not_put_in_the_query_string(client, monkeypatch) -> None:
    """
    The clause with teeth. api.data.gov says the key "should be kept
    private"; a query parameter is the one transport that does not, since
    it reaches server logs, proxy logs, Referer, browser history and
    requests' own exception messages - which is why the 403 handler had
    to be written to suppress the URL in the first place.
    """
    seen = {}

    class FakeResp:
        status_code = 200
        headers = {"X-RateLimit-Remaining": "1900",
                   "X-RateLimit-Limit": "2000"}

        def json(self):
            return {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params or {}
        return FakeResp()

    monkeypatch.setattr(client.session, "get", fake_get)
    monkeypatch.setattr(techport.time, "sleep", lambda _s: None)
    client.get("/projects", updatedSince="2020-01-01")

    assert "api_key" not in seen["params"], (
        f"the key is being passed as a query parameter: "
        f"{sorted(seen['params'])}"
    )
    assert KEY not in seen["url"]
    assert KEY not in str(seen["params"])


def test_rate_limit_is_read_from_the_server_not_asserted(client, monkeypatch) -> None:
    """
    The manual says 1,000/hour is the DEFAULT and that limits "may vary
    by service". This key reports ~2,000 on TechPort, so a hardcoded
    1,000 was a guess. The server states it on every response.
    """
    class FakeResp:
        status_code = 200
        headers = {"X-RateLimit-Remaining": "1897",
                   "X-RateLimit-Limit": "2000"}

        def json(self):
            return {}

    monkeypatch.setattr(client.session, "get",
                        lambda *a, **k: FakeResp())
    monkeypatch.setattr(techport.time, "sleep", lambda _s: None)
    client.get("/projects")
    assert client.limit == 2000
    assert client.remaining == 1897


def test_client_stops_before_the_limit_rather_than_at_it(client, monkeypatch) -> None:
    """
    Exceeding the limit temporarily blocks the KEY, not just the request.
    A floor makes that something we stay clear of rather than recover
    from.
    """
    assert techport.QUOTA_FLOOR > 0
    client.remaining = techport.QUOTA_FLOOR - 1

    def must_not_be_called(*a, **k):            # pragma: no cover
        raise AssertionError("a request was made below the quota floor")

    monkeypatch.setattr(client.session, "get", must_not_be_called)
    with pytest.raises(SystemExit) as e:
        client.get("/projects")
    assert "floor" in str(e.value)


def test_requests_pause_between_calls() -> None:
    assert techport.PAUSE_S > 0, (
        "a survey does not need to sprint; a pause is the difference "
        "between using a budget and consuming one"
    )


def test_documented_error_codes_are_the_ones_published() -> None:
    """
    Verified against the api.data.gov developer manual on 2026-09-14.
    API_KEY_UNVERIFIED was missing from the first version of the hint -
    it is the one that fires when the confirmation email was never
    clicked, which is exactly the state a new key sits in.
    """
    published = {
        "API_KEY_MISSING", "API_KEY_INVALID", "API_KEY_DISABLED",
        "API_KEY_UNAUTHORIZED", "API_KEY_UNVERIFIED", "HTTPS_REQUIRED",
        "OVER_RATE_LIMIT", "NOT_FOUND",
    }
    assert set(techport.API_UMBRELLA_CODES) == published


def test_cache_ttl_is_set_and_sane() -> None:
    assert techport.CACHE_TTL_S >= 24 * 3600, (
        "a TTL under a day would spend requests to observe a field that "
        "moves on the order of months"
    )


# ── the SEC seed file (added 2026-10-12) ─────────────────────────────

def test_every_sec_seed_row_states_a_basis() -> None:
    """Same rule as org_aliases.tsv, and it earned its keep immediately:
    the first version of sec_ciks.tsv had eight rows with a one-line
    basis, and read_seed() refused to load the file until they said
    something a reviewer could act on."""
    from src.funding.sec_edgar import SEED
    rows = [l for l in SEED.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")
            and not l.lower().startswith("ticker\t")]
    assert rows, "the SEC seed file is empty"
    for line in rows:
        parts = line.split("\t")
        assert len(parts) >= 4, f"no basis column: {line[:60]}"
        from src.funding.sec_edgar import DECISIONS
        assert parts[2] in DECISIONS, parts[2]
        assert len(parts[3].strip()) > 40, (
            f"basis too thin to review: {parts[0]} -> {parts[3][:40]!r}")


def test_sec_edgar_refuses_to_request_without_a_contact_address(monkeypatch):
    """The SEC's webmaster FAQ says "Please declare your user agent" and
    shows a sample carrying an admin contact address. It never calls the
    address REQUIRED -- an earlier version of this docstring said it did,
    which is the same error as citing a rate limit nobody published, run
    in reverse. So this refusal is THIS PROJECT'S choice: CONTACT_EMAIL
    is optional in the code base and ships blank in .env.example, and
    the Data Source Register's claim that useragent.py "already
    satisfies this" holds only once somebody sets it. Refusing beats
    sending a UA the SEC cannot reach and risking the IP block the
    standing instruction says to avoid."""
    import pytest
    from src.funding.sec_edgar import Client
    monkeypatch.setenv("CONTACT_EMAIL", "")
    with pytest.raises(SystemExit) as e:
        Client()
    assert "CONTACT_EMAIL" in str(e.value)


def test_sec_edgar_holds_itself_below_the_published_limit() -> None:
    """EDGAR publishes 10 req/s. A ceiling is not a target."""
    from src.funding.sec_edgar import MIN_INTERVAL_S
    assert MIN_INTERVAL_S >= 0.1, "that would be at or over the 10/s limit"
    assert MIN_INTERVAL_S >= 0.2, "the stated policy is half the ceiling"


def _concept(tag: str, years, name: str = "A FILER INC") -> dict:
    """A companyconcept payload carrying one FY 10-K fact per year."""
    return {"entityName": name, "units": {"USD": [
        {"fp": "FY", "form": "10-K", "start": f"{y}-01-01",
         "end": f"{y}-12-31", "filed": f"{y + 1}-02-07", "val": 1000 + y}
        for y in years]}}


def test_a_connection_error_never_carries_a_credential() -> None:
    """A connection failure is the one place a connection string can
    turn up in a message, and nothing in this project prints a
    credential. The URL is replaced WHOLESALE rather than masked in
    part, because a half-masked secret is still a secret someone has to
    reason about. The driver's own words have to survive, though: the
    whole point of showing the message is that "server didn't return
    client encoding" and "password authentication failed" mean
    different things, and check_db_endpoints.py tells them apart."""
    from src.funding.sec_edgar import _db_message

    class Fake(Exception):
        pass

    plain = Fake("server didn't return client encoding")
    assert _db_message(plain) == "server didn't return client encoding"

    leaky = Fake('could not connect to\n  postgresql://postgres.abc:'
                 'sup3r-s3cret@aws-0-us-east-1.pooler.supabase.com:6543/'
                 'postgres (timeout)')
    out = _db_message(leaky)
    for secret in ("sup3r-s3cret", "postgres.abc", "pooler.supabase.com",
                   "postgresql://"):
        assert secret not in out, f"{secret} survived: {out}"
    assert "<connection string redacted>" in out
    assert "\n" not in out, "one line, so it prints as one"

    wrapped = Fake("wrapper")
    wrapped.orig = plain
    assert _db_message(wrapped) == "server didn't return client encoding", (
        "the DBAPI message under SQLAlchemy's wrapper is the useful half")


def test_a_full_year_mislabelled_as_a_quarter_is_refused_and_reported():
    """L3Harris's FY2025 10-K, exactly as EDGAR serves it: revenue of
    $21,865,000,000 with start=2025-10-04, end=2026-01-02 -- fp="FY",
    form 10-K, and a **90-day span**. Nine months of FY2025 were
    $16.2bn, so the value is unmistakably the full year; its own dates
    say quarter. The SEC's frame machinery reads those dates the same
    way and files the fact under CY2025Q4, which is why the filer is
    absent from the CY2025 annual frame.

    Both halves of the handling are asserted, because each alone is a
    failure this project has already made. It must NOT be kept: a
    90-day span cannot be told from a quarter by its dates, and a
    quarter sitting in an annual series is a number four times too
    small with nothing to mark it. And it must NOT vanish: refusing it
    silently leaves --survey showing a series that stops in January
    2025 and nothing newer, which reads as "this filer has not filed"
    while the number sits in EDGAR (AD-113)."""
    from src.funding.sec_edgar import annual_facts
    payload = {"units": {"USD": [
        {"fp": "FY", "form": "10-K", "start": "2023-12-30",
         "end": "2025-01-03", "filed": "2025-02-14",
         "val": 21_325_000_000},
        {"fp": "FY", "form": "10-K", "start": "2025-10-04",
         "end": "2026-01-02", "filed": "2026-02-12",
         "val": 21_865_000_000},
    ]}}
    keep, why, near = annual_facts(payload, "AnyRevenueTag")
    assert [f["end"] for f in keep] == ["2025-01-03"], (
        "a 90-day span must not enter an annual series")
    assert why["span 90d"] == 1
    assert len(near) == 1, near
    assert near[0]["val"] == 21_865_000_000, (
        "the refused fact must come back, or the caller cannot tell a "
        "filer who has not filed from one whose dates are wrong")


def test_published_date_is_when_a_number_was_first_said() -> None:
    """A 10-K restates its two prior years, so one period's value
    appears in three filings and the supersede rule keeps the newest.
    That made published_date the date of the most recent filing to
    REPEAT a number rather than the one that stated it: L3Harris's
    FY2024 revenue would have published 2026-02-12, **405 days** after
    its period end, when it was first stated 2025-02-14. Across
    sixteen filers with ten to nineteen years of history that is not an
    outlier, it is most of the rows.

    The value still comes from the latest filing, because a restatement
    must win. first_said is the earliest filing carrying THAT value."""
    from src.funding.sec_edgar import annual_facts
    same = 21_325_000_000
    payload = {"units": {"USD": [
        {"fp": "FY", "form": "10-K", "start": "2023-12-30",
         "end": "2025-01-03", "filed": f, "val": same}
        for f in ("2026-02-12", "2025-02-14", "2027-02-10")]}}
    keep, why, _near = annual_facts(payload, "AnyRevenueTag")
    assert len(keep) == 1
    assert keep[0]["filed"] == "2027-02-10", "the value is the newest"
    assert keep[0]["first_said"] == "2025-02-14", (
        "published_date must be when the number was said, not when it "
        "was last repeated")
    assert why["repeated in a later filing"] == 2, (
        "the old counter only incremented when a NEWER fact displaced "
        "one already seen, so a payload in newest-first order counted "
        "none of them")
    assert "restated with a DIFFERENT value" not in why


def test_a_restated_value_publishes_on_the_filing_that_changed_it():
    """When a later filing changes the number, the earliest filing to
    say THAT number is the restatement itself. The original was a
    different claim about the same period -- which is a vintage event
    worth naming, not noise -- and the value kept was first said when
    the restatement said it."""
    from src.funding.sec_edgar import annual_facts
    payload = {"units": {"USD": [
        {"fp": "FY", "form": "10-K", "start": "2024-01-01",
         "end": "2024-12-31", "filed": "2025-02-01", "val": 100},
        {"fp": "FY", "form": "10-K", "start": "2024-01-01",
         "end": "2024-12-31", "filed": "2026-02-01", "val": 110},
    ]}}
    keep, why, _near = annual_facts(payload, "AnyRevenueTag")
    assert keep[0]["val"] == 110, "a restatement wins the value"
    assert keep[0]["first_said"] == "2026-02-01", (
        "the restated number was first said by the restatement")
    assert why["restated with a DIFFERENT value"] == 1, (
        "a number that changed between filings is a vintage event and "
        "is named separately from a repeat")


def test_an_http_error_keeps_the_url_and_drops_the_boilerplate() -> None:
    """Both directions of this were wrong inside one day. str(exc)[:50]
    cut a 404 off at `.../api/xbrl/compan`: the problem named and the
    URL needed to reproduce it gone. Printing the body whole then put
    300 characters of S3 XML into the middle of the trail it was printed
    into -- a RequestId and HostId nobody will look up, and a Key
    already in the URL beside it. Keep the server's own reason, on one
    line, next to the URL that produced it."""
    from src.funding.sec_edgar import _brief_body
    body = (b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>'
            b'NoSuchKey</Code><Message>The specified key does not exist.'
            b'</Message><Key>api/xbrl/frames/us-gaap/SalesRevenueNet/USD/'
            b'CY2025.json</Key><RequestId>6A0W88VM6E67KS20</RequestId>'
            b'<HostId>0WILON9nToutgJX60niRINM1CKY</HostId></Error>')
    brief = _brief_body(body)
    assert brief == "NoSuchKey: The specified key does not exist."
    assert "\n" not in brief, "a trail line has to stay one line"
    for noise in ("RequestId", "HostId", "<?xml", "<Code>"):
        assert noise not in brief, f"{noise} survived"
    assert len(_brief_body(b"x" * 5000)) <= 200, "an unbounded body"
    assert _brief_body(b"") == "", (
        "an empty body must read as empty, so the caller can omit the "
        "separator rather than print a dash with nothing after it")


def test_tag_chain_is_ordered_and_never_a_merge() -> None:
    """The one-tag-per-metric rule was replaced by an ordered chain on
    2026-10-19, because the first live --survey showed Lockheed's and
    Boeing's revenue series ending in 2019: both moved to `Revenues`
    and a single tag could not follow. --apply would have written
    $59.8bn as Lockheed's revenue, correctly stamped as_of 2019-12-31,
    against a real 2025 figure near $74bn. A stale number wearing a
    valid date is worse than a gap (AD-113, AD-116).

    What a chain must never become is a MERGE. One tag supplies the
    whole series, so: no tag twice in a chain (re-reading one concept
    could look like two opinions), no empty chain, and market_cap still
    absent because EDGAR files shares outstanding and no price."""
    from src.funding.sec_edgar import TAGS_FOR_METRIC
    for metric, chain in TAGS_FOR_METRIC.items():
        assert chain, f"{metric}: an empty chain reads as 'no data' forever"
        assert len(set(chain)) == len(chain), (
            f"{metric}: a tag appears twice in the chain: {chain}")
        assert all(isinstance(t, str) and t for t in chain), chain
    assert "market_cap" not in TAGS_FOR_METRIC, (
        "EDGAR has shares outstanding and no price; market cap would be "
        "derived, not published")


def test_source_url_names_the_concept_that_was_read() -> None:
    """The objection to a chain was real: source='sec_edgar' plus
    metric='revenue' would mean different things per filer, and
    cost_claims has no column saying which tag a row came from. The fix
    was to stop pointing source_url at a browse-edgar page -- which
    records nothing -- and point it at the companyconcept URL actually
    fetched, which CONTAINS the tag.

    So every tag in every chain must survive the round trip
    tag -> URL -> tag, or a claim could not say where it came from. And
    a browse-edgar URL must read as "no tag recorded", never as a tag:
    guessing one would be the same error one layer down (AD-112)."""
    from src.funding.sec_edgar import (CONCEPT, FILING_URL, TAGS_FOR_METRIC,
                                       tag_from_url)
    for metric, chain in TAGS_FOR_METRIC.items():
        for tag in chain:
            url = CONCEPT.format(cik=320193, tag=tag)
            assert tag_from_url(url) == tag, (
                f"{metric}/{tag}: source_url would not identify its own "
                f"concept; round trip gave {tag_from_url(url)!r}")
    assert tag_from_url(FILING_URL.format(cik=936468)) is None, (
        "a pre-2026-10-19 browse-edgar source_url must read as 'tag not "
        "recorded'")
    assert tag_from_url(None) is None and tag_from_url("") is None


def test_pick_series_reads_the_preferred_tag_and_stops(monkeypatch) -> None:
    """A fresh preferred tag must end the walk. Otherwise every filer
    costs a request per tag in the chain, and a rate limit honoured by
    halving the published ceiling is undone by quadrupling the calls."""
    from datetime import date
    from src.funding import sec_edgar as se

    chain = se.TAGS_FOR_METRIC["revenue"]
    asked: list = []

    def fake(_c, url, _key, _use_cache=True):
        asked.append(se.tag_from_url(url))
        return _concept(se.tag_from_url(url), (2023, 2024, 2025)), False

    monkeypatch.setattr(se, "cached", fake)
    s = se.pick_series(None, 320193, "revenue", False,
                       today=date(2026, 10, 19))
    assert s["tag"] == chain[0]
    assert asked == [chain[0]], f"read more than it needed: {asked}"
    assert len(s["facts"]) == 3


def test_pick_series_falls_past_a_stale_tag_without_merging(
        monkeypatch) -> None:
    """Lockheed's exact shape: the preferred tag carries 2017-2019 and
    nothing since; `Revenues` carries the recent years. The chain must
    return Revenues' series ALONE -- three facts, not six -- and name
    it. A spliced series is one no filer ever reported and no URL could
    cite, which is the opposite of what source_url is now for."""
    from datetime import date
    from src.funding import sec_edgar as se

    preferred, second = se.TAGS_FOR_METRIC["revenue"][:2]

    def fake(_c, url, _key, _use_cache=True):
        tag = se.tag_from_url(url)
        if tag == preferred:
            return _concept(tag, (2017, 2018, 2019)), False
        if tag == second:
            return _concept(tag, (2023, 2024, 2025)), False
        raise RuntimeError(f"HTTP 404 from {url}")

    monkeypatch.setattr(se, "cached", fake)
    s = se.pick_series(None, 936468, "revenue", False,
                       today=date(2026, 10, 19))
    assert s["tag"] == second, s["trail"]
    assert len(s["facts"]) == 3, "a chain chooses a tag, it does not merge"
    assert [f["end"] for f in s["facts"]] == [
        "2023-12-31", "2024-12-31", "2025-12-31"]
    assert s["stale_days"] <= se.STALE_SERIES_DAYS
    assert any(preferred in line for line in s["trail"]), (
        "the trail must show the tag that was passed over, so the "
        "decision is visible and not only its result")


def test_pick_series_keeps_the_preferred_tag_when_every_tag_is_stale(
        monkeypatch) -> None:
    """If nothing in the chain is current, the MOST PREFERRED tag that
    has data wins -- not the newest. An old fact carries its own real
    as_of_date and is still true; silently switching concepts because
    one happens to be a year less old would be a choice nobody could
    see. stale_days is set so the caller can say so out loud."""
    from datetime import date
    from src.funding import sec_edgar as se

    preferred, second = se.TAGS_FOR_METRIC["revenue"][:2]

    def fake(_c, url, _key, _use_cache=True):
        tag = se.tag_from_url(url)
        if tag == preferred:
            return _concept(tag, (2017, 2018)), False
        if tag == second:
            return _concept(tag, (2019, 2020)), False
        raise RuntimeError(f"HTTP 404 from {url}")

    monkeypatch.setattr(se, "cached", fake)
    s = se.pick_series(None, 936468, "revenue", False,
                       today=date(2026, 10, 19))
    assert s["tag"] == preferred, s["trail"]
    assert s["stale_days"] > se.STALE_SERIES_DAYS
    assert any(str(se.STALE_SERIES_DAYS) in line for line in s["trail"])


def test_pick_series_survives_a_404_on_the_preferred_tag(monkeypatch) -> None:
    """NOC's measured case: `revenue  no companyconcept (HTTP 404)`. A
    404 on the preferred tag is exactly what a chain is for, so it must
    be a trail line and the next tag must be tried -- not an exception
    that loses the filer, and not an empty result reported as 'no
    data' (AD-113)."""
    from datetime import date
    from src.funding import sec_edgar as se

    preferred, second = se.TAGS_FOR_METRIC["revenue"][:2]

    def fake(_c, url, _key, _use_cache=True):
        tag = se.tag_from_url(url)
        if tag == second:
            return _concept(tag, (2024, 2025)), False
        raise RuntimeError(f"HTTP 404 from {url}")

    monkeypatch.setattr(se, "cached", fake)
    s = se.pick_series(None, 1133421, "revenue", False,
                       today=date(2026, 10, 19))
    assert s["tag"] == second
    assert any("404" in line and preferred in line for line in s["trail"]), (
        f"the 404 must be readable in full, with its URL: {s['trail']}")
    assert any(url_part in " ".join(s["trail"])
               for url_part in ("data.sec.gov",)), (
        "a truncated error message is one you have to reproduce to read")


def test_pick_series_returns_nothing_rather_than_guessing(monkeypatch) -> None:
    """Every tag 404s. The answer is "no series", with the trail saying
    so for each tag -- not a tag with an empty list, which a caller
    could write as a provenance URL for facts it never read."""
    from src.funding import sec_edgar as se

    def fake(_c, url, _key, _use_cache=True):
        raise RuntimeError(f"HTTP 404 from {url}")

    monkeypatch.setattr(se, "cached", fake)
    s = se.pick_series(None, 1, "rd_spend", False)
    assert s["tag"] is None and s["facts"] == []
    assert len(s["trail"]) == len(se.TAGS_FOR_METRIC["rd_spend"])


def test_space_sic_codes_exist_in_the_secs_own_list() -> None:
    """SPACE_SIC shipped with "3761" and "3669", neither of which is in
    the SEC's Division of Corporation Finance SIC list. The space code is
    3760. Nothing filters on it today, so the bug was invisible; the
    moment something did, the query would return zero rows and read as an
    empty universe. These are the codes the SEC's list actually carries."""
    from src.funding.sec_edgar import SPACE_SIC
    assert "3760" in SPACE_SIC, "guided missiles & space vehicles is 3760"
    for absent in ("3761", "3669"):
        assert absent not in SPACE_SIC, (
            f"{absent} is not a code in the SEC's list")
    assert all(c.isdigit() and len(c) == 4 for c in SPACE_SIC), SPACE_SIC
    assert all(isinstance(v, str) and v for v in SPACE_SIC.values()), (
        "every code carries the SEC's own office description, so a "
        "reviewer can check it against the list without a lookup")


def test_consume_rows_are_recorded_but_never_ingested_as_claims():
    """`consume` was added 2026-10-19 and is not a softer refusal — it
    is a different axis. The other decisions answer "is this part of
    the space industry"; consume answers "does this business rest on
    something in orbit", which is the demand side.

    The hard part is what it must NOT do. Lockheed's revenue is money
    the space industry earned. Garmin's is money consumers paid for
    handheld GPS units, of which some unknown fraction reflects the
    value of a space service. Writing the second into cost_claims as
    though it were the first is a real number meaning something other
    than its column says — so --apply ingests match and claim only."""
    from src.funding import sec_edgar as se
    assert "consume" in se.DECISIONS
    assert "consume" not in se.INGESTABLE
    assert set(se.INGESTABLE) == {"match", "claim"}
    src = pathlib.Path(se.__file__).read_text(encoding="utf-8")
    # Every gate that decides whether to fetch financial facts must use
    # INGESTABLE, not a hand-listed comparison that a fifth decision
    # would quietly fall through (AD-109).
    assert src.count("decision not in INGESTABLE") >= 2
