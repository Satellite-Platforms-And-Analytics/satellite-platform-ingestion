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
        "key\turl\tcap\tcadence_minutes\tverified\tverified_on\tbasis\n"
        "x\thttps://e.test/f.xml\tMONEY\t10\tyes\t\t"
        + "a basis long enough to clear the forty character floor\n",
        encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        watch.read_registry(p)
    assert "no date" in str(e.value)


def test_an_http_feed_is_refused(tmp_path):
    p = tmp_path / "feeds.tsv"
    p.write_text(
        "key\turl\tcap\tcadence_minutes\tverified\tverified_on\tbasis\n"
        "x\thttp://e.test/f.xml\tMONEY\t10\tno\t\t"
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
    assert "a feed URL recalled from memory is a guess" in after
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


# ── the check that is normally skipped ───────────────────────────────

def test_a_feed_months_behind_its_own_stated_cadence_is_late():
    """Built from the real measurement: three SEC feeds each describing
    themselves as updated every ten minutes, last built in April and
    February 2026, read on 2026-10-13."""
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    channel = {"built": datetime(2026, 4, 24, 22, 50, tzinfo=timezone.utc)}
    verdict, sentence = watch.staleness(channel, 10, now=now)
    assert verdict == "LATE"
    assert "not being updated" in sentence


def test_a_fresh_feed_inside_its_cadence_is_ok():
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    channel = {"built": now - timedelta(minutes=12)}
    assert watch.staleness(channel, 10, now=now)[0] == "ok"


def test_a_feed_with_no_build_date_is_reported_as_uncheckable():
    """Not 'ok'. A feed that cannot be checked for staleness is a
    different thing from a feed that passed the check, and collapsing
    the two is how an empty result starts sounding like a finding."""
    verdict, sentence = watch.staleness({"built": None}, 0)
    assert verdict == "UNDATED"
    assert "cannot be checked" in sentence


def test_an_unstated_cadence_still_has_a_ceiling():
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    old = {"built": now - timedelta(days=200)}
    assert watch.staleness(old, 0, now=now)[0] == "LATE"


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
                      "INSERT", "cost_claims", "engine", "commit"):
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
