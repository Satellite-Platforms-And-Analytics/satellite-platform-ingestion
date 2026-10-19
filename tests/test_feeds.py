"""
Tests for the feed watcher.

Nothing here touches the network. The fixtures are cut from the real
payloads measured on 2026-10-13 - including the SEC's Atom-namespace
typo and its edgar: filing metadata - because a parser tested against a
tidied-up fixture is a parser tested against a feed that does not exist.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from src.feeds import watch


# ── fixtures, cut from real payloads ─────────────────────────────────

#: Shape measured at https://www.sec.gov/Archives/edgar/usgaap.rss.xml.
#: Note xmlns:atom="https://www.w3.org/2005/Atom" - the Atom namespace
#: is http, not https. That is the SEC's typo, reproduced deliberately:
#: parsing on local names means it cannot break us.
SEC_RSS = """<?xml version="1.0" encoding="ISO-8859-1"?>
<rss version="2.0" xmlns:atom="https://www.w3.org/2005/Atom"
     xmlns:edgar="https://www.sec.gov/Archives/edgar">
 <channel>
  <title>Filings containing financial statements tagged using the US GAAP or IFRS taxonomies.</title>
  <link>https://www.sec.gov/spotlight/xbrl/filings-and-feeds.shtml</link>
  <atom:link href="https://www.sec.gov/Archives/edgar/usgaap.rss.xml" rel="self" type="application/rss+xml"/>
  <description>This is a list of up to 200 of the latest filings, updated every 10 minutes.</description>
  <language>en-us</language>
  <pubDate>Fri, 24 Apr 2026 18:50:03 EDT</pubDate>
  <lastBuildDate>Fri, 24 Apr 2026 18:50:03 EDT</lastBuildDate>
  <item>
   <title>NewHydrogen, Inc. (0001371128) (Filer)</title>
   <link>https://www.sec.gov/Archives/edgar/data/1371128/000149315226018998/0001493152-26-018998-index.htm</link>
   <guid>https://www.sec.gov/Archives/edgar/data/1371128/000149315226018998/0001493152-26-018998-index.htm</guid>
   <description>POS AM</description>
   <pubDate>Fri, 24 Apr 2026 17:31:42 EDT</pubDate>
   <edgar:xbrlFiling>
    <edgar:companyName>NewHydrogen, Inc.</edgar:companyName>
    <edgar:formType>POS AM</edgar:formType>
    <edgar:filingDate>04/24/2026</edgar:filingDate>
    <edgar:cikNumber>0001371128</edgar:cikNumber>
    <edgar:accessionNumber>0001493152-26-018998</edgar:accessionNumber>
    <edgar:assignedSic>3081</edgar:assignedSic>
   </edgar:xbrlFiling>
  </item>
  <item>
   <title>Some Space Company (0000936468) (Filer)</title>
   <link>https://www.sec.gov/Archives/edgar/data/936468/x-index.htm</link>
   <guid>https://www.sec.gov/Archives/edgar/data/936468/x-index.htm</guid>
   <description>10-K</description>
   <pubDate>Fri, 24 Apr 2026 17:02:00 EDT</pubDate>
   <edgar:xbrlFiling>
    <edgar:companyName>Some Space Company</edgar:companyName>
    <edgar:formType>10-K</edgar:formType>
    <edgar:cikNumber>0000936468</edgar:cikNumber>
    <edgar:assignedSic>3760</edgar:assignedSic>
   </edgar:xbrlFiling>
  </item>
 </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <title>Example filings</title>
 <subtitle>Latest</subtitle>
 <updated>2026-10-13T11:00:00Z</updated>
 <entry>
  <title>10-K - EXAMPLE CORP</title>
  <link rel="alternate" type="text/html" href="https://example.gov/a"/>
  <id>urn:tag:example.gov,2026:accession-number=0000-26-1</id>
  <updated>2026-10-13T10:45:00Z</updated>
 </entry>
</feed>
"""


# ── the registry ──────────────────────────────────────────────────────

def test_the_shipped_registry_parses_and_every_row_states_a_basis():
    """read_registry() raises on a thin basis, so this passing IS the
    assertion that every row says why it is there."""
    feeds = watch.read_registry()
    assert len(feeds) >= 5
    for f in feeds:
        assert len(f.basis) > 40
        assert f.cap in watch.CAPS
        assert f.url.startswith("https://")


def test_a_row_claiming_verified_without_a_date_is_refused(tmp_path):
    """A measurement without a date is a memory. AD-112 was a recalled
    identifier that nobody had re-read; the date column is what makes
    re-reading possible."""
    p = tmp_path / "feeds.tsv"
    p.write_text(
        "key\turl\tcap\tcadence_claimed\tstale_after_hours\twindow"
        "\tverified\tverified_on\tbasis\n"
        "x\thttps://e.test/f.xml\tMONEY\tunstated\t24\t\tyes\t\t"
        + "a basis long enough to clear the forty character floor\n",
        encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        watch.read_registry(p)
    assert "no date" in str(e.value)


def test_an_http_feed_is_refused(tmp_path):
    p = tmp_path / "feeds.tsv"
    p.write_text(
        "key\turl\tcap\tcadence_claimed\tstale_after_hours\twindow"
        "\tverified\tverified_on\tbasis\n"
        "x\thttp://e.test/f.xml\tMONEY\tunstated\t24\t\tno\t\t"
        + "a basis long enough to clear the forty character floor\n",
        encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        watch.read_registry(p)
    assert "not https" in str(e.value)


def test_writing_the_registry_keeps_its_documentation(tmp_path):
    """The header comment is longer than the data and is the file's only
    explanation of what verified=no means. Rebuilding the file from
    parsed rows would silently delete it."""
    src = watch.SEED.read_text(encoding="utf-8")
    p = tmp_path / "feeds.tsv"
    p.write_text(src, encoding="utf-8")
    feeds = watch.read_registry(p)
    feeds[0].verified, feeds[0].verified_on = "yes", "2026-10-13"
    watch.write_registry(feeds, p)
    after = p.read_text(encoding="utf-8")
    # Count comment lines rather than look for one sentence. The first
    # version of this test asserted a specific phrase and failed the
    # moment the header was legitimately rewritten - a test that pins
    # prose stops the documentation from being improved.
    assert (len([l for l in after.splitlines() if l.startswith("#")])
            == len([l for l in src.splitlines() if l.startswith("#")]) > 20)
    assert watch.read_registry(p)[0].verified == "yes"
    assert after.count("\n") == src.count("\n")


# ── parsing ───────────────────────────────────────────────────────────

def test_rss_parses_including_the_edgar_filing_metadata():
    channel, items = watch.parse_feed(SEC_RSS)
    assert channel["format"] == "rss"
    assert channel["built"] == datetime(2026, 4, 24, 18, 50, 3,
                                        tzinfo=timezone(timedelta(hours=-4)))
    assert len(items) == 2
    assert items[1]["form"] == "10-K"
    assert items[1]["cik"] == "0000936468"
    assert items[1]["sic"] == "3760", (
        "the SIC is the joinable half of a filing feed; without it an "
        "item is a headline")
    assert items[0]["id"].endswith("-index.htm")


def test_atom_parses_and_takes_its_link_from_the_href_attribute():
    channel, items = watch.parse_feed(ATOM)
    assert channel["format"] == "atom"
    assert len(items) == 1
    assert items[0]["link"] == "https://example.gov/a"
    assert items[0]["id"].startswith("urn:tag:")
    assert items[0]["published"].year == 2026


def test_a_wrong_atom_namespace_does_not_break_parsing():
    """The SEC declares xmlns:atom as https://www.w3.org/2005/Atom. The
    namespace is http. Matching local names means someone else's typo is
    not our outage - and a parser bound to namespace URIs would have
    returned zero items, which reads as a quiet feed."""
    _, items = watch.parse_feed(SEC_RSS)
    assert len(items) == 2


def test_an_html_error_page_is_not_a_feed():
    """A 200 serving HTML is the failure --verify guards against: it
    looks like success at the HTTP layer and yields no items."""
    with pytest.raises(Exception):
        watch.parse_feed("<html><body>Request Rate Threshold "
                         "Exceeded</body></html>")


# ── staleness, rebuilt on what the first real run measured ──────────

def _feed(stale_after_hours=20.0, window=""):
    return watch.Feed(key="t", url="https://e.test/f.xml", cap="MONEY",
                      cadence_claimed="every 10 min Mon-Fri 6am-10pm EST",
                      stale_after_hours=stale_after_hours, window=window,
                      verified="yes", verified_on="2026-10-13",
                      basis="a basis long enough to clear the floor rule")


SEC_WINDOW = "Mon-Fri 06:00-22:00 America/New_York"


def test_the_false_late_that_the_first_real_run_produced():
    """THE regression test for this module.

    At 23:34 ET on Monday 2026-10-12 the first version reported both SEC
    XBRL feeds LATE. They had last been built at 21:50 ET - the last
    ten-minute slot before the 10pm close the SEC publishes. The check
    had kept "every ten minutes" and discarded "Monday through Friday,
    6am - 10pm EST", then scored the publisher against a multiplier this
    project invented. Nothing was wrong with the feeds.
    """
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    built = datetime(2026, 10, 12, 21, 50, 3, tzinfo=et)
    now = datetime(2026, 10, 12, 23, 34, 0, tzinfo=et)
    verdict, sentence = watch.staleness(
        {"built": built}, _feed(window=SEC_WINDOW), now=now)
    assert verdict == "ok", sentence
    assert "inside" in sentence


def test_overnight_and_weekend_silence_is_not_silence():
    """Friday 21:55 to Monday 07:00 is 57 wall-clock hours and one
    in-window hour. Measuring against the wall clock makes every
    Saturday look like an outage."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    built = datetime(2026, 10, 9, 21, 55, tzinfo=et)      # Friday
    now = datetime(2026, 10, 12, 7, 0, tzinfo=et)         # Monday
    assert (now - built).total_seconds() / 3600 > 57
    inside = watch.open_seconds(built, now, SEC_WINDOW) / 3600
    assert 1.0 <= inside <= 1.1, inside
    assert watch.staleness({"built": built},
                           _feed(window=SEC_WINDOW), now=now)[0] == "ok"


def test_a_feed_that_really_stops_is_still_caught():
    """The window must not become a blanket excuse. Two full in-window
    weekdays of no rebuild is 32 hours and is reported."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    built = datetime(2026, 10, 7, 6, 5, tzinfo=et)        # Wednesday
    now = datetime(2026, 10, 9, 21, 0, tzinfo=et)         # Friday
    verdict, sentence = watch.staleness(
        {"built": built}, _feed(window=SEC_WINDOW), now=now)
    assert verdict == "LATE", sentence
    assert "means nothing" in sentence


def test_one_closed_weekday_does_not_trip_the_sec_threshold():
    """Holidays are not modelled. The shipped threshold of 20h is
    chosen so a single closed weekday - 16 in-window hours, which is
    what Columbus Day 2026-10-12 would have looked like had the SEC
    stopped rebuilding - cannot raise an alarm on its own."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    built = datetime(2026, 10, 9, 21, 55, tzinfo=et)      # Friday close
    now = datetime(2026, 10, 12, 22, 0, tzinfo=et)        # Monday close
    inside = watch.open_seconds(built, now, SEC_WINDOW) / 3600
    assert 15.9 <= inside <= 16.1, inside
    shipped = next(f for f in watch.read_registry()
                   if f.key == "sec_xbrl_usgaap")
    assert shipped.stale_after_hours > inside, (
        "the shipped threshold would fire on one closed weekday")


def test_item_age_is_not_checked_at_all():
    """Both SEC feeds' newest ITEM on 10-13 was Friday 10-09, because
    10-10 and 10-11 were the weekend and Monday 10-12 was Columbus Day,
    a federal holiday on which EDGAR accepts no filings. A feed rebuilt
    on time and correctly containing nothing is healthy, so staleness
    reads lastBuildDate and never an item date."""
    import inspect
    src = inspect.getsource(watch.staleness)
    assert "items" not in src and "published" not in src


def test_a_feed_with_no_build_date_is_reported_as_uncheckable():
    """Not 'ok'. Measured: both SEC news feeds carry no lastBuildDate,
    so they are permanently uncheckable, and collapsing that into ok is
    how an empty result starts sounding like a finding."""
    verdict, sentence = watch.staleness({"built": None}, _feed())
    assert verdict == "UNDATED"
    assert "cannot be checked" in sentence


def test_a_windowless_feed_counts_every_hour():
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    old = {"built": now - timedelta(hours=200)}
    assert watch.staleness(old, _feed(168.0), now=now)[0] == "LATE"
    recent = {"built": now - timedelta(hours=100)}
    assert watch.staleness(recent, _feed(168.0), now=now)[0] == "ok"


def test_a_window_spec_that_cannot_be_parsed_is_refused_not_guessed():
    """A guessed window silently stops counting hours a feed was
    actually silent, which is worse than having no window."""
    for bad in ("Mon-Fri 06:00-22:00", "Mon-Fri 6-10 America/New_York",
                "Xxx-Fri 06:00-22:00 America/New_York",
                "Mon-Fri 06:00-22:00 Mars/Olympus"):
        with pytest.raises(SystemExit):
            watch.parse_window(bad)
    watch.parse_window(SEC_WINDOW)          # the real one still works


def test_the_registry_demands_a_threshold_with_an_author(tmp_path):
    """stale_after_hours is OURS. The publisher's claim sits beside it
    in its own column so the two can never again be the same number by
    accident."""
    head = ("key\turl\tcap\tcadence_claimed\tstale_after_hours\twindow"
            "\tverified\tverified_on\tbasis\n")
    basis = "a basis long enough to clear the forty character floor"
    p = tmp_path / "feeds.tsv"
    p.write_text(head + f"x\thttps://e.test/f.xml\tMONEY\tunstated\t0\t"
                 f"\tno\t\t{basis}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        watch.read_registry(p)
    assert "died" in str(e.value)
    p.write_text(head + f"x\thttps://e.test/f.xml\tMONEY\t\t24\t"
                 f"\tno\t\t{basis}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        watch.read_registry(p)
    assert "cadence_claimed" in str(e.value)


# ── the guardrail ─────────────────────────────────────────────────────

def test_the_watcher_cannot_write_to_the_database():
    """A feed item is evidence that a claim might exist, not a claim.
    The architectural steer says money data will be built up from many
    sources including news, by inference - which is an argument for a
    person or a reasoning module writing cost_claims with a
    derivation_basis, and against this module growing an --ingest flag.
    Asserted in code because a docstring saying so is not a control
    (AD-098: writing down a failure mode is not avoiding it)."""
    import tokenize
    # Comments and docstrings DISCUSS cost_claims at length - that
    # discussion is the point. Strip every comment and string literal
    # so the assertion is about executable code only. The first version
    # of this test stripped '#' lines alone and failed on its own
    # subject's docstring.
    with tokenize.open(watch.__file__) as fh:
        code = " ".join(
            t.string for t in tokenize.generate_tokens(fh.readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING))
    for forbidden in ("src.db", "sqlalchemy", "psycopg", "DATABASE_URL",
                      "INSERT", "cost_claims", "engine", "commit",
                      # sec_edgar imports src.db.writer and sqlalchemy at
                      # module level and calls bootstrap(). Importing it
                      # for SPACE_SIC would drag a database engine
                      # factory in through the back door, which is why
                      # the constant moved to src/sic.py on 2026-10-14.
                      "sec_edgar"):
        assert forbidden not in code, (
            f"{forbidden!r} appears in watch.py's executable code: this "
            f"module reports, it does not ingest")


def test_conditional_get_headers_are_sent_when_state_has_them():
    """304 Not Modified is the whole politeness argument: the steady
    state of watching ten feeds should transfer no bodies at all."""
    import inspect
    src = inspect.getsource(watch.Client.get_feed)
    assert "If-None-Match" in src and "If-Modified-Since" in src
    assert "304" in src


def test_robots_refusal_is_not_overridable_from_the_command_line():
    """The SEC's own RSS page recommends a Latest Filings Atom feed on
    /cgi-bin/browse-edgar, a path the session's fetcher refused citing
    the target's robots rules. The standing instruction is to violate no
    site policy, so there is deliberately no --ignore-robots flag to
    reach for at 2am."""
    src = pathlib.Path(watch.__file__).read_text(encoding="utf-8")
    assert "--ignore-robots" not in src
    assert "--no-robots" not in src


# ── robots.txt, the three outcomes ────────────────────────────────────

class _FakeResponse:
    def __init__(self, status: int, text: str = ""):
        self.status_code, self.text = status, text
        self.content = text.encode("utf-8")   # a real response has both
        self.headers: dict = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise watch.requests.HTTPError(str(self.status_code))


class _FakeSession:
    """Answers one canned response and records what was asked for."""

    def __init__(self, response):
        self.headers = {"User-Agent": "satellite-platform/1.0 (test)"}
        self._response = response
        self.asked: list = []

    def get(self, url, headers=None, timeout=None):
        self.asked.append(url)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _client_with(response):
    c = watch.Client()
    c.s = _FakeSession(response)
    return c


def test_robots_200_rules_are_obeyed():
    body = "User-agent: *\nDisallow: /cgi-bin/\nCrawl-delay: 5\n"
    c = _client_with(_FakeResponse(200, body))
    allowed, why = c.robots("https://e.test/cgi-bin/browse-edgar?output=atom")
    assert allowed is False and "DISALLOWS" in why
    # Cached: a second question about the same host costs no request.
    spent = c.spent
    assert c.robots("https://e.test/Archives/feed.xml")[0] is True
    assert c.spent == spent
    assert c._delay["e.test"] == 5.0, "a published Crawl-delay is honoured"


def test_robots_4xx_means_no_rules_exist():
    """RFC 9309 2.3.1.4: on 4xx "crawlers MAY access any resources"."""
    c = _client_with(_FakeResponse(404))
    allowed, why = c.robots("https://e.test/feed.xml")
    assert allowed is True and "4xx" in why


def test_robots_unreachable_means_assume_disallow():
    """RFC 9309 2.3.1.3: on 5xx "crawlers SHOULD assume complete
    disallow". The first version of this module treated unreachable as
    permissive and said so in a docstring that misquoted the RFC; it was
    caught by running --verify behind an egress proxy that answers 403
    for every host, which printed "treating as no rules" for a host it
    had never spoken to."""
    for response in (_FakeResponse(503),
                     watch.requests.RequestException("proxy refused")):
        c = _client_with(response)
        allowed, why = c.robots("https://e.test/feed.xml")
        assert allowed is False, response
        assert "assume disallow" in why or "unknown" in why


def test_conditional_get_sends_nothing_on_a_first_look():
    c = _client_with(_FakeResponse(200, SEC_RSS))
    f = watch.read_registry()[0]
    status, body, got = c.get_feed(f, {})
    assert status == 200 and body and got == {}


def test_check_refuses_http_and_refuses_a_non_feed(capsys):
    """--check is how the registry grows. It must refuse exactly what
    --verify refuses, or it becomes the loose door into the file."""
    c = _client_with(_FakeResponse(200, "<html>Request Access</html>"))
    with pytest.raises(SystemExit):
        watch.cmd_check(c, "http://e.test/f.xml")
    c = _client_with(_FakeResponse(200, "<html>Request Access</html>"))
    c._robots["e.test"] = None
    assert watch.cmd_check(c, "https://e.test/f.xml") == 1
    assert "NOT A FEED" in capsys.readouterr().out


def test_check_accepts_a_real_feed_without_writing_anything(capsys):
    c = _client_with(_FakeResponse(200, SEC_RSS))
    c._robots["e.test"] = None
    before = watch.SEED.read_text(encoding="utf-8")
    assert watch.cmd_check(c, "https://e.test/f.xml") == 0
    out = capsys.readouterr().out
    assert "2 item(s)" in out and "lastBuildDate" in out
    assert watch.SEED.read_text(encoding="utf-8") == before


def test_the_rate_floor_matches_the_strictest_rule_any_source_publishes():
    """arXiv's API terms, read at info.arxiv.org/help/api/tou.html on
    2026-10-14: "make no more than one request every three seconds, and
    limit requests to a single connection at a time", applying to "all
    of the machines under your control as a whole".

    MIN_INTERVAL_S was 2.0 before arXiv was added, which would have
    been a violation on the second arXiv request of any run. One global
    floor set at the strictest published rule cannot be undercut by a
    feed added later inheriting a faster default - the alternative, a
    per-host table of exceptions, is a thing somebody forgets to update
    (AD-109)."""
    assert watch.MIN_INTERVAL_S >= 3.0, (
        "arXiv publishes 1 request / 3 seconds; a faster floor breaks it")


def test_a_registry_feed_on_arxiv_is_covered_by_that_floor():
    feeds = {f.key: f for f in watch.read_registry()}
    arxiv = [f for f in feeds.values() if "arxiv" in f.host]
    assert arxiv, "the arXiv row is gone; is the 3s floor still justified?"
    for f in arxiv:
        assert "info.arxiv.org/help/api/tou" in f.basis, (
            f"{f.key}'s basis must name where its terms were read")


# ── zero items, and the day the module failed its own thesis ─────────

def test_zero_items_on_a_feed_that_has_held_items_is_a_fault():
    """2026-10-14: --verify printed "HTTP 200  rss  0 item(s)" and then
    "ok", for arXiv. Freshly built, correctly titled, conditional-GET
    headers offered, and containing nothing. The only health check read
    dates, so an empty feed passed - the exact failure this module
    exists to prevent, committed by the module itself."""
    verdict, sentence = watch.emptiness([], seen_max=200)
    assert verdict == "EMPTY"
    assert "stopped matching" in sentence


def test_zero_items_with_no_history_says_so_rather_than_guessing():
    """Zero is not automatically a fault, and the difference is history,
    not a rule: a feed never seen holding items has nothing to be
    compared against."""
    verdict, sentence = watch.emptiness([], seen_max=0)
    assert verdict == "FIRST-EMPTY"
    assert "nothing to" in sentence
    assert watch.emptiness([{"id": "x"}], seen_max=0)[0] == "ok"


def test_a_fully_turned_over_window_is_reported_as_a_sampler():
    """Measured: sec_xbrl_all reported 200 new of 200 items one day
    after its 200-item baseline. Every id had changed, so the items in
    between are gone. Reporting "200 new" without this invites the
    reading that 200 things happened."""
    msg = watch.turnover(200, 200)
    assert msg and "sampler" in msg
    assert watch.turnover(93, 200) is None


# ── the SIC digest split ─────────────────────────────────────────────

def test_the_sic_split_finds_the_one_filing_that_mattered():
    """The real shape of the 10-14 poll: Morgan Stanley prospectuses
    burying one in-scope filing. 3663 is in the SEC's list; 6211 and
    2834 are not."""
    items = [{"sic": "6211", "title": "MORGAN STANLEY"} for _ in range(8)]
    items += [{"sic": "2834", "title": "Apimeds Pharmaceuticals"},
              {"sic": "3663", "title": "Ondas Inc."}]
    in_scope, other, coded = watch.split_by_sic(items)
    assert [i["title"] for i in in_scope] == ["Ondas Inc."]
    assert len(other) == 9 and coded is True
    assert "6211x8" in watch._top_sics(other)


def test_items_without_a_sic_are_never_filtered_away():
    """NASA items carry no SIC. A filter that silently dropped them
    would turn an EVENT digest into an empty one."""
    items = [{"sic": "", "title": "NASA launches something"}]
    in_scope, other, coded = watch.split_by_sic(items)
    assert in_scope == [] and other == items
    assert coded is False, (
        "an uncoded feed must be distinguishable from a coded feed with "
        "nothing in scope, or the digest prints one as the other")


def test_the_sic_list_has_exactly_one_definition():
    """Two copies of an identifier list, drifting, one of them wrong, is
    AD-112 with the serial numbers filed off. src/sic.py is the only
    place the codes are written down."""
    import pathlib
    root = pathlib.Path(watch.__file__).resolve().parents[2] / "src"
    defining = [p for p in root.rglob("*.py")
                if "SPACE_SIC = {" in p.read_text(encoding="utf-8")]
    assert [p.name for p in defining] == ["sic.py"], defining
    from src.sic import SPACE_SIC as canonical
    from src.funding.sec_edgar import SPACE_SIC as reexported
    assert canonical is reexported


def test_coded_but_nothing_in_scope_is_a_third_case():
    """Measured 2026-10-14: six new usgaap items, all coded, none in
    scope - pharma, finance, oil and gas. "0 of 6 in scope" is the
    honest line; printing the six implies they were judged relevant."""
    items = [{"sic": s, "title": t} for s, t in
             (("2834", "Alaunos Therapeutics"), ("6199", "SkyAI"),
              ("1311", "EON Resources"))]
    in_scope, other, coded = watch.split_by_sic(items)
    assert in_scope == [] and coded is True and len(other) == 3


def test_verify_does_not_promote_a_feed_that_yields_no_items(tmp_path):
    """The first version of emptiness() printed EMPTY and then promoted
    the row anyway - the fault reported, and the surrounding code acting
    as though it had not been (AD-109). --verify --apply would have
    flipped the arXiv row back to verified=yes on every run."""
    import inspect
    src = inspect.getsource(watch.cmd_verify)
    assert "NOT PROMOTED" in src
    i = src.index("if not items:")
    j = src.index('f.verified = "yes"', i)
    assert 'f.verified, f.verified_on = "no", ""' in src[i:j], (
        "the empty-feed branch must clear verified before the promotion "
        "line is reached")


def test_an_uncoded_digest_actually_PRINTS_its_items(capsys, monkeypatch):
    """The bug this test exists for printed "nasa_news (10 new)" and
    then listed nothing, for four days, because

        show = in_scope if (in_scope or not coded) else []

    evaluates the condition True for an uncoded feed and then returns
    in_scope, which is []. Every NASA digest was empty.

    test_items_without_a_sic_are_never_filtered_away passed throughout:
    it tested split_by_sic, which was correct. The caller was wrong.
    So this test drives cmd_poll and reads what a human would read."""
    feed = watch.Feed(
        key="nasa_news", url="https://e.test/f.xml", cap="EVENT",
        cadence_claimed="unstated", stale_after_hours=168.0, window="",
        verified="yes", verified_on="2026-10-13",
        basis="a basis long enough to clear the forty character floor")
    body = ATOM.replace(
        "<title>10-K - EXAMPLE CORP</title>",
        "<title>NASA Science, Cargo Launch on 35th Resupply</title>")

    c = _client_with(_FakeResponse(200, body))
    monkeypatch.setattr(watch, "load_state",
                        lambda: {"nasa_news": {"seen": ["old-id"]}})
    monkeypatch.setattr(watch, "save_state", lambda s: None)
    c._robots["e.test"] = None
    watch.cmd_poll([feed], c, include_unverified=False, write=False)
    out = capsys.readouterr().out
    assert "1 new" in out
    assert "NASA Science, Cargo Launch" in out, (
        "the item was counted and not shown: the digest is hiding its "
        "own effect")
