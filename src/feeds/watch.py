"""
Feed watcher — one place that notices new information, politely.

Why this exists
---------------
Every source this project reads answers "what is true now". None of them
answers "what changed since I last looked", and so far the only way this
project has found out that something changed is by re-downloading the
thing and diffing it. That is how 13,517 gp_history re-downloads
happened on 09-14. A feed is the publisher telling us what is new, in
one request, and three of the sources already in the Data Source
Register publish one.

The point of this module is NOT to ingest. It is to answer, for every
source in the register, exactly two questions:

    1. has anything new appeared?
    2. is this feed still alive?

Question 2 is the one that is normally skipped, and it is the one that
bites. A watcher that only reports new items cannot tell "nothing
happened" from "the feed died six months ago" - both look like an empty
list. That is AD-112's shape exactly: an empty result stating a
falsehood confidently. So `lastBuildDate` against the publisher's own
declared cadence is a first-class check here, and a feed that is late is
a reported FAULT, not a quiet zero.

It bit immediately. On 2026-10-13 the SEC's three XBRL feeds, each
describing itself as "updated every 10 minutes", came back with
lastBuildDate values of 24 Apr 2026, 24 Apr 2026 and 27 Feb 2026 - two
agreeing to the second, one seven weeks earlier. Measured through the
session's fetcher, which is not the network this pipeline runs on, so it
is recorded as something to re-measure rather than a fact about the SEC.
Either way the registry ships those rows as verified=no and this module
is built to say so out loud.

Politeness, since the standing instruction is not to get blocked
-----------------------------------------------------------------
Feeds are the cheapest thing a publisher serves, and conditional GET is
why: send the ETag and Last-Modified we were given, and an unchanged
feed answers 304 with no body at all. This module always sends them, so
the steady state of watching ten feeds is ten requests that transfer
nothing. A watcher without conditional GET re-downloads every feed every
run and is indistinguishable from a scraper.

On top of that:

  - robots.txt is read and OBEYED per host, with Crawl-delay honoured
    where one is published. This is not decoration. The SEC's own RSS
    page recommends its "Latest Filings" Atom feed at
    /cgi-bin/browse-edgar?action=getcurrent&output=atom, and the
    session's fetcher refused that URL citing the target's robots
    rules - a recommended feed on a disallowed path. This module
    resolves that by asking robots.txt itself at poll time instead of
    taking anyone's word for it, including mine.
  - src.useragent, so a request says who is calling and how often.
  - one request per feed per run, a hard cap per run, and a per-host
    minimum interval.
  - no feed is polled at all unless the registry says somebody verified
    its URL, because polling a URL nobody has checked is how you find
    out what a 404 loop looks like from the other end.

A feed item is not a claim
--------------------------
Nothing here writes to the database. That is deliberate and it is the
part most likely to be "improved" later by mistake. The architectural
steer says cost and budget information will come from many sources
including news, built up over time by inference. It does NOT say a
headline is a number. An item from nasa_news saying a contract was
awarded is evidence that a claim might exist; the claim itself belongs
in cost_claims with a derivation_basis naming this item, written by a
person or by a module that can state its reasoning. Automatic
extraction from news into money rows would manufacture confident
figures with no vintage and no author, which is the one thing the claims
model exists to prevent.

Deliberately NOT done
---------------------
  - No feed_items table. Until the digest has been read a few times we
    do not know what the schema should be, and a table is harder to
    delete than a JSON file.
  - No full-text fetch of linked articles. One request per feed is the
    whole budget; following every link is a crawl.
  - No feedparser dependency. requirements.txt is audited against real
    imports and a tenth package that stdlib xml.etree can cover is not
    worth the audit.
  - No scheduling. `--poll` is for a human or a scheduled workflow to
    run; this module does not own a clock.
"""
from __future__ import annotations

import argparse
import email.utils
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

_root = str(pathlib.Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)

from src.useragent import user_agent          # noqa: E402

SEED = (pathlib.Path(__file__).resolve().parent.parent.parent
        / "data" / "seed" / "feeds.tsv")
CACHE_DIR = (pathlib.Path(__file__).resolve().parent.parent.parent
             / "data" / "cache" / "feeds")
STATE = CACHE_DIR / "state.json"

#: Minimum seconds between two requests to the SAME host. Overridden
#: upward, never downward, by a Crawl-delay in that host's robots.txt.
MIN_INTERVAL_S = 2.0

#: Hard ceiling per run, robots.txt fetches included. Ten feeds plus
#: four hosts' robots is 14; this leaves room without leaving room for
#: a loop.
DEFAULT_MAX_REQUESTS = 40

#: How many of its own declared cadences a feed may fall behind before
#: this module calls it broken. A feed claiming 10-minute updates is not
#: interesting at 20 minutes late and is certainly interesting at an
#: hour; 6 is the compromise, and the number is here rather than inline
#: so that arguing with it means editing one line.
STALE_FACTOR = 6

#: For a feed whose publisher states no cadence, there is nothing to be
#: late against, so silence is judged against this instead. Chosen to be
#: longer than any plausible quiet spell at NASA and shorter than the
#: six months the SEC's feeds were measured at.
UNSTATED_CADENCE_STALE_DAYS = 30

#: Keep the state file bounded. Per feed, remember this many item ids.
#: The SEC feeds hold 200 items, so 500 survives several quiet runs
#: without ever letting an old id come back as new.
SEEN_PER_FEED = 500

CAPS = {"OBJ", "ORG", "MONEY", "EVENT", "TECH", "POLICY"}


# ── the registry ──────────────────────────────────────────────────────

class Feed:
    __slots__ = ("key", "url", "cap", "cadence_minutes",
                 "verified", "verified_on", "basis")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc

    def __repr__(self) -> str:
        return f"<Feed {self.key} {self.cap} verified={self.verified}>"


def read_registry(path: pathlib.Path = SEED) -> "list[Feed]":
    """Parse feeds.tsv, refusing a row a reviewer could not act on.

    The basis-length rule is the same one org_aliases.tsv and
    sec_ciks.tsv carry, and it has now caught thin rows in all three
    files - including eight of my own in sec_ciks.tsv, which is the only
    reason it is trusted here.
    """
    feeds: "list[Feed]" = []
    seen_keys: "set[str]" = set()
    for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.rstrip("\n")
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if parts[0] == "key":
            continue
        if len(parts) != 7:
            raise SystemExit(
                f"{path.name}:{lineno}: expected 7 tab-separated columns, "
                f"got {len(parts)}. A registry that parses loosely is a "
                f"registry that silently drops a source.")
        key, url, cap, cadence, verified, verified_on, basis = parts
        if key in seen_keys:
            raise SystemExit(f"{path.name}:{lineno}: duplicate key {key!r}")
        seen_keys.add(key)
        if cap not in CAPS:
            raise SystemExit(
                f"{path.name}:{lineno}: cap {cap!r} is not one of "
                f"{sorted(CAPS)}")
        if not url.startswith("https://"):
            raise SystemExit(
                f"{path.name}:{lineno}: {key} is not https. A feed read "
                f"over http can be rewritten in transit, and this "
                f"project's cybersecurity constraint is standing.")
        if verified not in ("yes", "no"):
            raise SystemExit(
                f"{path.name}:{lineno}: verified must be yes or no, "
                f"got {verified!r}")
        if verified == "yes" and not verified_on.strip():
            raise SystemExit(
                f"{path.name}:{lineno}: {key} claims verified=yes with no "
                f"date. A measurement without a date is a memory.")
        try:
            cadence_i = int(cadence)
        except ValueError:
            raise SystemExit(
                f"{path.name}:{lineno}: cadence_minutes {cadence!r} is not "
                f"an integer (0 means the publisher states none)")
        if len(basis.strip()) <= 40:
            raise SystemExit(
                f"{path.name}:{lineno}: {key}'s basis is too thin to "
                f"review: {basis.strip()!r}")
        feeds.append(Feed(key=key, url=url, cap=cap,
                          cadence_minutes=cadence_i, verified=verified,
                          verified_on=verified_on.strip(),
                          basis=basis.strip()))
    if not feeds:
        raise SystemExit(f"{path.name} lists no feeds")
    return feeds


def write_registry(feeds: "list[Feed]", path: pathlib.Path = SEED) -> None:
    """Rewrite only the verified/verified_on columns, keeping comments.

    The header comment block is the file's documentation and is longer
    than the data. Rewriting the file from the parsed rows would throw
    it away, so this edits matched lines in place instead.
    """
    by_key = {f.key: f for f in feeds}
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        parts = raw.split("\t")
        if (not raw.startswith("#") and len(parts) == 7
                and parts[0] in by_key):
            f = by_key[parts[0]]
            parts[4] = f.verified
            parts[5] = f.verified_on
            out.append("\t".join(parts))
        else:
            out.append(raw)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ── state ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE.is_file():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass            # an unreadable state file is not an error;
    return {}               # it means every item is new this once.


def save_state(state: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1, sort_keys=True),
                         encoding="utf-8")
    except OSError:
        print("  ! could not write state; next run will re-report items")


# ── HTTP ──────────────────────────────────────────────────────────────

class Client:
    """One session, one budget, one clock per host."""

    def __init__(self, max_requests: int = DEFAULT_MAX_REQUESTS):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": user_agent(
                "feed watching; <=1 request per feed per run",
                f"<=1 req/{MIN_INTERVAL_S:.0f}s per host"),
            # A feed is XML, and gzip is the publisher's cheapest win.
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/rss+xml, application/atom+xml, "
                      "application/xml, text/xml, */*",
        })
        self.budget = max_requests
        self.spent = 0
        self._last: "dict[str, float]" = {}
        self._delay: "dict[str, float]" = {}
        # host -> RobotFileParser (rules), None (4xx, no rules), or
        # the string "unreachable" (5xx/network: assume disallow).
        self._robots: "dict[str, object]" = {}

    def _wait(self, host: str) -> None:
        gap = max(MIN_INTERVAL_S, self._delay.get(host, 0.0))
        last = self._last.get(host)
        if last is not None:
            due = last + gap - time.monotonic()
            if due > 0:
                time.sleep(due)
        self._last[host] = time.monotonic()

    def _spend(self, n: int = 1) -> None:
        if self.spent + n > self.budget:
            raise SystemExit(
                f"\nRequest budget of {self.budget} exhausted. Raise it "
                f"with --max-requests if that is really what you want; "
                f"a watcher that needs more than one request per feed is "
                f"a watcher with a bug.")
        self.spent += n

    def robots(self, url: str) -> "tuple[bool, str]":
        """(allowed, why). One robots.txt fetch per host per run.

        The three outcomes are NOT the same, and RFC 9309 section 2.3.1
        says which is which:

          2xx  parse and obey the rules, Crawl-delay included.
          4xx  "crawlers MAY access any resources" - no rules exist, so
               fetching is allowed.
          5xx or a network error
               "crawlers SHOULD assume complete disallow".

        The first version of this method collapsed 4xx and unreachable
        into one permissive branch, and its own docstring claimed that
        was what the RFC said. It is not: a host we cannot reach is a
        host whose rules we do not know, and guessing "allowed" there is
        precisely the behaviour that earns an IP block. Found by running
        --verify from an environment whose egress proxy answers 403 for
        everything, which printed "treating as no rules" for a host it
        had never spoken to.
        """
        host = urllib.parse.urlsplit(url).netloc
        if host not in self._robots:
            robots_url = f"https://{host}/robots.txt"
            self._spend()
            self._wait(host)
            try:
                r = self.s.get(robots_url, timeout=20)
            except requests.RequestException as e:
                self._robots[host] = "unreachable"
                print(f"  robots.txt for {host}: unreachable "
                      f"({e.__class__.__name__}) - RFC 9309 says assume "
                      f"complete disallow, so this host is skipped")
            else:
                if r.status_code == 200:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
                    self._robots[host] = rp
                    d = rp.crawl_delay(self.s.headers["User-Agent"])
                    if d is None:
                        d = rp.crawl_delay("*")
                    if d:
                        self._delay[host] = float(d)
                        print(f"  robots.txt for {host}: "
                              f"Crawl-delay {d}s, honoured")
                elif 400 <= r.status_code < 500:
                    self._robots[host] = None
                    print(f"  robots.txt for {host}: HTTP "
                          f"{r.status_code}, so no rules exist")
                else:
                    self._robots[host] = "unreachable"
                    print(f"  robots.txt for {host}: HTTP "
                          f"{r.status_code} - RFC 9309 says assume "
                          f"complete disallow, so this host is skipped")
        rp = self._robots[host]
        if rp == "unreachable":
            return False, ("robots.txt could not be read, so its rules are "
                           "unknown and RFC 9309 says assume disallow")
        if rp is None:
            return True, "no robots.txt (4xx), so no rules to break"
        ua = self.s.headers["User-Agent"]
        if rp.can_fetch(ua, url) and rp.can_fetch("*", url):
            return True, "robots.txt allows it"
        return False, "robots.txt DISALLOWS this path"

    def get_feed(self, feed: Feed, state: dict) -> "tuple[int, str|None, dict]":
        """Conditional GET. Returns (status, body_or_None, new_headers).

        304 returns no body and is the healthy steady state.
        """
        st = state.get(feed.key, {})
        headers = {}
        if st.get("etag"):
            headers["If-None-Match"] = st["etag"]
        if st.get("last_modified"):
            headers["If-Modified-Since"] = st["last_modified"]
        self._spend()
        self._wait(feed.host)
        r = self.s.get(feed.url, headers=headers, timeout=30)
        got = {}
        if r.headers.get("ETag"):
            got["etag"] = r.headers["ETag"]
        if r.headers.get("Last-Modified"):
            got["last_modified"] = r.headers["Last-Modified"]
        if r.status_code == 304:
            return 304, None, got
        r.raise_for_status()
        return r.status_code, r.text, got


# ── parsing ───────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Local name, namespace discarded. The SEC declares the Atom
    namespace as https://www.w3.org/2005/Atom, which is not the Atom
    namespace - it is http. Matching on local names means this module
    does not care, and a namespace typo in someone else's feed is not
    our outage."""
    return tag.rpartition("}")[2]


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _find(parent, name: str):
    for ch in parent:
        if _local(ch.tag) == name:
            return ch
    return None


def _parse_date(s: str) -> "datetime|None":
    """RSS dates are RFC 822, Atom dates are RFC 3339, and some feeds
    carry a timezone abbreviation that neither parser accepts."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_feed(body: str) -> "tuple[dict, list[dict]]":
    """Return (channel, items) for RSS 2.0 or Atom, without caring which.

    Each item is {id, title, link, published, form, cik, sic} - the last
    three populated only from the SEC's edgar: namespace, and left blank
    everywhere else rather than inferred.
    """
    root = ET.fromstring(body.strip())
    name = _local(root.tag)
    if name == "rss":
        chan = _find(root, "channel")
        if chan is None:
            raise ValueError("rss element with no channel")
        entries = [c for c in chan if _local(c.tag) == "item"]
        channel = {
            "title": _text(_find(chan, "title")),
            "description": _text(_find(chan, "description")),
            "built": _parse_date(_text(_find(chan, "lastBuildDate")))
                     or _parse_date(_text(_find(chan, "pubDate"))),
            "format": "rss",
        }
        id_names, date_names = ("guid", "link"), ("pubDate",)
    elif name == "feed":
        entries = [c for c in root if _local(c.tag) == "entry"]
        channel = {
            "title": _text(_find(root, "title")),
            "description": _text(_find(root, "subtitle")),
            "built": _parse_date(_text(_find(root, "updated"))),
            "format": "atom",
        }
        id_names, date_names = ("id", "link"), ("updated", "published")
    else:
        raise ValueError(f"not a feed: root element is {name!r}")

    items = []
    for e in entries:
        ident = ""
        for n in id_names:
            el = _find(e, n)
            if el is not None:
                ident = _text(el) or (el.get("href") or "")
                if ident:
                    break
        published = None
        for n in date_names:
            published = _parse_date(_text(_find(e, n)))
            if published:
                break
        link_el = _find(e, "link")
        link = (_text(link_el) or (link_el.get("href") if link_el is not None
                                   else "") or "")
        item = {"id": ident or link, "title": _text(_find(e, "title")),
                "link": link, "published": published,
                "form": "", "cik": "", "sic": ""}
        # The SEC's XBRL feeds carry structured filing metadata inside
        # edgar:xbrlFiling. Reading it costs nothing and turns a headline
        # into something joinable - which is the difference between a
        # feed worth watching and a news ticker.
        filing = _find(e, "xbrlFiling")
        if filing is not None:
            item["form"] = _text(_find(filing, "formType"))
            item["cik"] = _text(_find(filing, "cikNumber"))
            item["sic"] = _text(_find(filing, "assignedSic"))
        items.append(item)
    return channel, items


# ── the check that is normally skipped ────────────────────────────────

def staleness(channel: dict, cadence_minutes: int,
              now: "datetime|None" = None) -> "tuple[str, str]":
    """(verdict, sentence). verdict is ok | LATE | UNDATED.

    A feed that states its own cadence has volunteered the test. Three
    SEC feeds describing themselves as "updated every 10 minutes" were
    measured months behind on 2026-10-13; nothing in an item list says
    so, and a watcher reporting "0 new" would have been both correct and
    useless.
    """
    now = now or datetime.now(timezone.utc)
    built = channel.get("built")
    if built is None:
        return "UNDATED", ("no lastBuildDate or updated element, so this "
                           "feed cannot be checked for staleness at all")
    behind = now - built
    if cadence_minutes > 0:
        limit = timedelta(minutes=cadence_minutes * STALE_FACTOR)
        claim = f"publisher states {cadence_minutes} min"
    else:
        limit = timedelta(days=UNSTATED_CADENCE_STALE_DAYS)
        claim = f"no stated cadence, judged against {UNSTATED_CADENCE_STALE_DAYS}d"
    human = _ago(behind)
    if behind > limit:
        return "LATE", (f"last built {human} ago ({claim}) - this feed is "
                        f"not being updated, so an empty result from it "
                        f"means nothing")
    return "ok", f"last built {human} ago ({claim})"


def _ago(d: timedelta) -> str:
    s = int(d.total_seconds())
    if s < 0:
        return f"-{_ago(-d)}"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


# ── modes ─────────────────────────────────────────────────────────────

def cmd_list(feeds: "list[Feed]") -> None:
    print(f"\n{len(feeds)} feed(s) in {SEED.name}\n")
    print(f"  {'key':<18}{'cap':<8}{'cadence':<10}{'verified':<12}host")
    print("  " + "-" * 74)
    for f in feeds:
        cad = f"{f.cadence_minutes}m" if f.cadence_minutes else "unstated"
        ver = f.verified_on if f.verified == "yes" else "NO"
        print(f"  {f.key:<18}{f.cap:<8}{cad:<10}{ver:<12}{f.host}")
    unver = [f.key for f in feeds if f.verified != "yes"]
    if unver:
        print(f"\n  {len(unver)} unverified, skipped by --poll: "
              f"{', '.join(unver)}")
        print("  Run --verify from the network the pipeline runs on.")


def cmd_verify(feeds: "list[Feed]", c: Client, only: "list[str]|None",
               apply_: bool) -> int:
    """Fetch every candidate once, report what came back, and record it.

    This is the only mode that may change the registry, and it writes
    exactly two columns. A URL is promoted to verified=yes only if it
    returned 200 and PARSED - a 200 serving an HTML error page is the
    failure this guards.
    """
    print("\nVERIFY - one request per feed, from this machine's network\n")
    faults = 0
    for f in feeds:
        if only and f.key not in only:
            continue
        print(f"  {f.key}  {f.url}")
        allowed, why = c.robots(f.url)
        if not allowed:
            print(f"    REFUSED: {why}")
            print(f"    Not fetched. A feed this project is not permitted "
                  f"to read is not a source, whoever recommends it.")
            f.verified, f.verified_on = "no", ""
            faults += 1
            continue
        try:
            status, body, got = c.get_feed(f, {})
        except requests.RequestException as e:
            print(f"    FAILED: {e.__class__.__name__}: {e}")
            f.verified, f.verified_on = "no", ""
            faults += 1
            continue
        if body is None:
            print(f"    HTTP {status} with no body on a first fetch - "
                  f"unexpected, treating as unverified")
            faults += 1
            continue
        try:
            channel, items = parse_feed(body)
        except (ET.ParseError, ValueError) as e:
            print(f"    HTTP {status}, {len(body)} bytes, DID NOT PARSE: {e}")
            print(f"    A 200 is not a feed. Left unverified.")
            f.verified, f.verified_on = "no", ""
            faults += 1
            continue
        verdict, sentence = staleness(channel, f.cadence_minutes)
        print(f"    HTTP {status}  {channel['format']}  "
              f"{len(items)} item(s)  conditional-GET headers: "
              f"{'yes' if got else 'NONE OFFERED'}")
        print(f"    title: {channel['title'][:68]}")
        print(f"    {verdict}: {sentence}")
        if items and items[0]["published"]:
            print(f"    newest item: {items[0]['published'].date()}  "
                  f"{items[0]['title'][:52]}")
        if verdict == "LATE":
            faults += 1
        f.verified = "yes"
        f.verified_on = datetime.now(timezone.utc).date().isoformat()
        print()
    print(f"  requests spent: {c.spent}/{c.budget}")
    if apply_:
        write_registry(feeds)
        print(f"  registry updated: {SEED}")
    else:
        print("  registry NOT written (pass --apply to record these results)")
    if faults:
        print(f"\n  {faults} fault(s) above. A LATE or unparseable feed is "
              f"a finding about the source, not about this module.")
    return faults


def cmd_poll(feeds: "list[Feed]", c: Client, include_unverified: bool,
             write: bool) -> None:
    state = load_state()
    todo = [f for f in feeds
            if f.verified == "yes" or include_unverified]
    skipped = [f.key for f in feeds if f not in todo]
    print(f"\nPOLL - {len(todo)} feed(s)"
          + (f", {len(skipped)} skipped as unverified" if skipped else ""))
    if skipped and not include_unverified:
        print(f"  skipped: {', '.join(skipped)}")
    if not todo:
        print("\n  Nothing to poll. Every row in the registry is "
              "unverified.\n  Run:  python -m src.feeds.watch --verify "
              "--apply")
        return

    by_cap: "dict[str, list[tuple[Feed, list[dict]]]]" = {}
    notes: "list[str]" = []
    unchanged = 0
    for f in todo:
        allowed, why = c.robots(f.url)
        if not allowed:
            notes.append(f"{f.key}: REFUSED - {why}")
            continue
        st = state.setdefault(f.key, {})
        try:
            status, body, got = c.get_feed(f, state)
        except requests.RequestException as e:
            notes.append(f"{f.key}: {e.__class__.__name__}: {e}")
            continue
        st.update(got)
        st["last_checked"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        if status == 304:
            unchanged += 1
            continue
        try:
            channel, items = parse_feed(body)
        except (ET.ParseError, ValueError) as e:
            notes.append(f"{f.key}: 200 but not a feed - {e}")
            continue
        verdict, sentence = staleness(channel, f.cadence_minutes)
        if verdict != "ok":
            notes.append(f"{f.key}: {verdict} - {sentence}")
        if channel.get("built"):
            st["last_build"] = channel["built"].isoformat()
        seen = set(st.get("seen", []))
        fresh = [i for i in items if i["id"] and i["id"] not in seen]
        first_run = not seen
        if first_run:
            # Everything is new on a first run, which is not news. Record
            # the ids and report the count rather than printing 200 items
            # and calling them today's developments.
            notes.append(f"{f.key}: first run, {len(items)} item(s) "
                         f"recorded as the baseline, none reported as new")
        elif fresh:
            by_cap.setdefault(f.cap, []).append((f, fresh))
        ids = [i["id"] for i in items if i["id"]]
        st["seen"] = (ids + st.get("seen", []))[:SEEN_PER_FEED]

    print(f"  {unchanged} unchanged (HTTP 304, no body transferred), "
          f"{c.spent} request(s) spent\n")

    total = sum(len(x[1]) for v in by_cap.values() for x in v)
    if total:
        print(f"  NEW SINCE LAST RUN - {total} item(s)")
        for cap in sorted(by_cap):
            print(f"\n  == {cap} ==")
            for f, fresh in by_cap[cap]:
                print(f"\n  {f.key}  ({len(fresh)} new)")
                for i in fresh[:12]:
                    when = (i["published"].date().isoformat()
                            if i["published"] else "undated")
                    tag = ""
                    if i["form"]:
                        tag = f"  [{i['form']} SIC {i['sic'] or '?'}]"
                    print(f"    {when}  {i['title'][:62]}{tag}")
                if len(fresh) > 12:
                    print(f"    ... and {len(fresh) - 12} more")
    else:
        print("  No new items.")
        if notes:
            print("  Note the faults below before reading that as quiet.")

    if notes:
        print("\n  FAULTS AND NOTES")
        for n in notes:
            print(f"    - {n}")

    if write:
        save_state(state)
    else:
        print("\n  state NOT written (--dry-run): the same items will be "
              "reported as new next run")


def main(argv: "list[str]|None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.feeds.watch",
        description="Watch the project's feeds for new information.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="show the registry, make no requests")
    g.add_argument("--verify", action="store_true",
                   help="fetch each feed once and record what came back")
    g.add_argument("--poll", action="store_true",
                   help="report items new since the last poll")
    p.add_argument("--only", nargs="*", metavar="KEY",
                   help="restrict --verify to these registry keys")
    p.add_argument("--apply", action="store_true",
                   help="--verify: write the results into feeds.tsv")
    p.add_argument("--dry-run", action="store_true",
                   help="--poll: do not record what was seen")
    p.add_argument("--include-unverified", action="store_true",
                   help="--poll: poll rows nobody has verified. Says what "
                        "it does.")
    p.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    a = p.parse_args(argv)

    feeds = read_registry()
    if a.list:
        cmd_list(feeds)
        return 0
    c = Client(max_requests=a.max_requests)
    if a.verify:
        return 1 if cmd_verify(feeds, c, a.only, a.apply) else 0
    cmd_poll(feeds, c, a.include_unverified, not a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
