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

from src.sic import SPACE_SIC                 # noqa: E402
from src.useragent import user_agent          # noqa: E402

SEED = (pathlib.Path(__file__).resolve().parent.parent.parent
        / "data" / "seed" / "feeds.tsv")
CACHE_DIR = (pathlib.Path(__file__).resolve().parent.parent.parent
             / "data" / "cache" / "feeds")
STATE = CACHE_DIR / "state.json"

#: Minimum seconds between two requests to the SAME host. Overridden
#: upward, never downward, by a Crawl-delay in that host's robots.txt.
#:
#: 3.0 because that is the STRICTEST published rule among the sources in
#: the registry, and a single floor at the strictest rule cannot be
#: undercut by a feed added later that inherits a faster default. The
#: rule is arXiv's, read at info.arxiv.org/help/api/tou.html on
#: 2026-10-14: "make no more than one request every three seconds, and
#: limit requests to a single connection at a time", and the limits
#: "apply to all of the machines under your control as a whole". It was
#: 2.0 until arXiv was added, which would have been a violation on the
#: second arXiv request of any run. Nothing published by the SEC or NASA
#: asks for more than this, so one number serves all of them and costs
#: about fifteen seconds a run.
#:
#: The "all machines under your control" clause is worth remembering:
#: if anything else on this network polls arXiv, this floor is not the
#: whole picture.
MIN_INTERVAL_S = 3.0

#: Hard ceiling per run, robots.txt fetches included. Ten feeds plus
#: four hosts' robots is 14; this leaves room without leaving room for
#: a loop.
DEFAULT_MAX_REQUESTS = 40

#: There is deliberately NO multiplier here any more.
#:
#: The first version derived its staleness threshold from the
#: publisher's claimed cadence times six. Both halves of that were
#: wrong on the first real run. The claimed cadence was taken from the
#: phrase "updated every ten minutes" while the rest of the SEC's own
#: sentence - "Monday through Friday, 6am - 10pm EST" - was dropped,
#: and the multiplier was a number this project invented and then
#: reported as though the publisher had been measured against their own
#: rule. Each feed now carries an explicit stale_after_hours in the
#: registry: a threshold with a visible author, informed by the
#: publisher's claim and not equal to it. See AD-115.

#: Keep the state file bounded. Per feed, remember this many item ids.
#: The SEC feeds hold 200 items, so 500 survives several quiet runs
#: without ever letting an old id come back as new.
SEEN_PER_FEED = 500

CAPS = {"OBJ", "ORG", "MONEY", "EVENT", "TECH", "POLICY"}


# ── the registry ──────────────────────────────────────────────────────

class Feed:
    __slots__ = ("key", "url", "cap", "cadence_claimed",
                 "stale_after_hours", "window", "verified", "verified_on",
                 "basis")

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
        if len(parts) != 9:
            raise SystemExit(
                f"{path.name}:{lineno}: expected 9 tab-separated columns, "
                f"got {len(parts)}. A registry that parses loosely is a "
                f"registry that silently drops a source.")
        (key, url, cap, cadence_claimed, stale_after, window, verified,
         verified_on, basis) = parts
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
            stale_h = float(stale_after)
        except ValueError:
            raise SystemExit(
                f"{path.name}:{lineno}: stale_after_hours {stale_after!r} "
                f"is not a number. This is OUR threshold, not the "
                f"publisher's cadence, and every feed needs one stated.")
        if stale_h <= 0:
            raise SystemExit(
                f"{path.name}:{lineno}: {key} has stale_after_hours "
                f"{stale_h}. A feed with no staleness threshold cannot be "
                f"distinguished from a feed that died.")
        if window.strip():
            parse_window(window)        # raises SystemExit on a bad spec
        if not cadence_claimed.strip():
            raise SystemExit(
                f"{path.name}:{lineno}: {key} states no cadence_claimed. "
                f"Write what the publisher says, verbatim enough to "
                f"check, or 'unstated' - the threshold beside it is only "
                f"reviewable against the claim it was chosen from.")
        if len(basis.strip()) <= 40:
            raise SystemExit(
                f"{path.name}:{lineno}: {key}'s basis is too thin to "
                f"review: {basis.strip()!r}")
        feeds.append(Feed(key=key, url=url, cap=cap,
                          cadence_claimed=cadence_claimed.strip(),
                          stale_after_hours=stale_h,
                          window=window.strip(), verified=verified,
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
        if (not raw.startswith("#") and len(parts) == 9
                and parts[0] in by_key):
            f = by_key[parts[0]]
            parts[6] = f.verified
            parts[7] = f.verified_on
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

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_window(spec: str) -> "tuple[set[int], int, int, str]":
    """Parse "Mon-Fri 06:00-22:00 America/New_York".

    Returns (weekdays, open_minute, close_minute, tzname). Raises
    SystemExit on anything it does not understand, because a window it
    guesses at is worse than no window: it would silently stop counting
    the hours a feed was actually silent.
    """
    bad = SystemExit(
        f"bad window spec {spec!r}. Expected e.g. "
        f"'Mon-Fri 06:00-22:00 America/New_York'")
    parts = spec.split()
    if len(parts) != 3:
        raise bad
    days, hours, tzname = parts
    try:
        d0, d1 = (d.strip().lower()[:3] for d in days.split("-"))
        i0, i1 = _DAYS.index(d0), _DAYS.index(d1)
        o, c = hours.split("-")
        oh, om = (int(x) for x in o.split(":"))
        ch, cm = (int(x) for x in c.split(":"))
    except (ValueError, IndexError):
        raise bad
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tzname)
    except Exception:
        raise SystemExit(f"unknown time zone {tzname!r} in window {spec!r}")
    wanted = set(range(i0, i1 + 1)) if i0 <= i1 else (
        set(range(i0, 7)) | set(range(0, i1 + 1)))
    return wanted, oh * 60 + om, ch * 60 + cm, tzname


def open_seconds(a: datetime, b: datetime, spec: str) -> float:
    """Seconds between a and b that fall INSIDE the window.

    A publisher that only rebuilds a feed 6am-10pm on weekdays has not
    gone quiet at midnight, and measuring silence against the wall clock
    makes every Saturday look like an outage. Day-by-day rather than
    clever, and capped, because a feed four years stale does not need
    its downtime computed precisely - it needs to be reported.
    """
    if not spec:
        return (b - a).total_seconds()
    if b <= a:
        return 0.0
    from zoneinfo import ZoneInfo
    days, open_m, close_m, tzname = parse_window(spec)
    tz = ZoneInfo(tzname)
    a_l, b_l = a.astimezone(tz), b.astimezone(tz)
    if (b_l - a_l).days > 400:
        return float("inf")
    total = 0.0
    day = a_l.date()
    while day <= b_l.date():
        if day.weekday() in days:
            start = datetime.combine(day, datetime.min.time(), tzinfo=tz) \
                + timedelta(minutes=open_m)
            end = datetime.combine(day, datetime.min.time(), tzinfo=tz) \
                + timedelta(minutes=close_m)
            lo, hi = max(start, a_l), min(end, b_l)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def staleness(channel: dict, feed: "Feed",
              now: "datetime|None" = None) -> "tuple[str, str]":
    """(verdict, sentence). verdict is ok | LATE | UNDATED.

    Measured against the feed's OWN stale_after_hours, counting only
    time inside its operating window.

    The first version of this function derived its threshold from the
    publisher's claimed cadence times a multiplier this project
    invented, and ignored the operating window entirely. On its first
    real run - 23:34 ET on Monday 2026-10-12 - it reported both SEC
    XBRL feeds as LATE because they were last built at 21:50 ET. The
    SEC publishes those feeds "every ten minutes Monday through Friday,
    6am - 10pm EST". 21:50 is the last ten-minute slot before close.
    The feeds could not have been more compliant, and the check called
    them dead.

    Two further things that run established, both of which this
    function must not mistake for faults:

      - The newest ITEM in both feeds was Friday 2026-10-09, because
        10-10 and 10-11 were the weekend and Monday 10-12 was Columbus
        Day, a federal holiday on which EDGAR accepts no filings. A
        feed that is rebuilt on time and correctly contains nothing is
        healthy. Item age is therefore not checked here at all.
      - The two SEC news feeds carry no lastBuildDate element, so they
        are UNDATED: uncheckable, which is reported as its own verdict
        rather than folded into ok.
    """
    now = now or datetime.now(timezone.utc)
    built = channel.get("built")
    if built is None:
        return "UNDATED", ("no lastBuildDate or updated element, so this "
                           "feed cannot be checked for staleness at all")
    inside = open_seconds(built, now, feed.window)
    limit = feed.stale_after_hours * 3600
    wall = _ago(now - built)
    if feed.window:
        detail = (f"{_ago(timedelta(seconds=inside))} of that inside "
                  f"{feed.window}")
    else:
        detail = "no operating window declared, so all of it counts"
    if inside > limit:
        return "LATE", (f"last built {wall} ago, {detail}, over this "
                        f"feed's {feed.stale_after_hours:g}h threshold - "
                        f"an empty result from it means nothing")
    return "ok", (f"last built {wall} ago, {detail}, within "
                  f"{feed.stale_after_hours:g}h")


def emptiness(items: list, seen_max: int) -> "tuple[str, str]":
    """(verdict, sentence). verdict is ok | EMPTY | FIRST-EMPTY.

    Added 2026-10-14, because `--verify` had just printed

        HTTP 200  rss  0 item(s)
        ok: last built 0m ago ... within 72h

    for the arXiv feed. Freshly built, correctly titled, conditional-GET
    headers offered, and **containing nothing** - and the module called
    it ok, because its only health check read dates. That is the exact
    failure this module was written to prevent, committed by the module
    itself three days running (AD-105, AD-112, AD-113). A feed with zero
    items cannot be distinguished from a query that matches nothing, and
    "ok" is the one word that must not describe it.

    Zero is not automatically a fault, though, and the difference is
    history rather than a rule: a feed that has never been seen holding
    items has nothing to be compared against, while a feed that held 200
    yesterday and holds 0 today has said something. So the maximum item
    count ever seen is kept in state and the verdict follows the
    measurement, not an assumption.
    """
    n = len(items)
    if n:
        return "ok", f"{n} item(s)"
    if seen_max > 0:
        return "EMPTY", (f"0 items, and this feed has held as many as "
                         f"{seen_max} before - either it is between "
                         f"cycles or the query stopped matching, and "
                         f"nothing here can tell those apart")
    return "FIRST-EMPTY", ("0 items, and no item count has ever been "
                           "recorded for it - so there is nothing to "
                           "compare against yet. Poll again after its "
                           "next build before believing the URL")


def turnover(new_count: int, total: int) -> "str|None":
    """A sentence when the whole window turned over, else None.

    Measured 2026-10-14: `sec_xbrl_all` reported **200 new of 200
    items** one day after its 200-item baseline was recorded. Every id
    had changed. The SEC accepts thousands of filings a day and the feed
    holds 200, so at a daily poll the window rotates completely and the
    items in between are simply gone.

    This matters more than it looks. It means a 200-item window on a
    firehose is a SAMPLING instrument, not a change log, and reporting
    "200 new" invites exactly the opposite reading. Completeness for
    MONEY comes from the bulk zips and companyconcept, as already
    recorded for USAspending (AD-060); this feed answers "something is
    happening", at best.
    """
    if total and new_count >= total:
        return (f"the ENTIRE {total}-item window is new, so items "
                f"between polls were missed - this feed is a sampler at "
                f"this poll rate, not a change log. Poll nearer the "
                f"publisher's cadence or use the bulk path for "
                f"completeness")
    return None


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

def split_by_sic(items: "list[dict]") -> "tuple[list, list]":
    """(in_scope, other) for items that carry a SIC; ([], items) if none do.

    Added after the 2026-10-14 poll printed 301 items, of which roughly
    six were about space. `sec_xbrl_all`'s first twelve were eight
    Morgan Stanley 424B2 prospectuses; the one filing that mattered,
    `Ondas Inc. [8-K SIC 3663]`, was further down a list nobody would
    have read to the end of.

    The filter changes WHAT A HUMAN IS SHOWN. It does not decide what is
    true, and the out-of-scope items are always counted out loud with
    their top SICs, so it cannot hide its own effect - which is the
    whole difference between a digest and a silent drop.
    """
    coded = [i for i in items if i.get("sic")]
    if not coded:
        return [], items
    in_scope = [i for i in items if i.get("sic") in SPACE_SIC]
    return in_scope, [i for i in items if i not in in_scope]


def _top_sics(items: "list[dict]", n: int = 4) -> str:
    from collections import Counter
    c = Counter(i.get("sic") or "?" for i in items)
    if not c:
        return ""
    top = ", ".join(f"{k}x{v}" for k, v in c.most_common(n))
    return f" (top: {top})"


def cmd_list(feeds: "list[Feed]") -> None:
    print(f"\n{len(feeds)} feed(s) in {SEED.name}\n")
    print(f"  {'key':<18}{'cap':<7}{'stale>':>7}  {'verified':<12}"
          f"{'window':<38}publisher says")
    print("  " + "-" * 112)
    for f in feeds:
        ver = f.verified_on if f.verified == "yes" else "NO"
        print(f"  {f.key:<18}{f.cap:<7}{f.stale_after_hours:>6.0f}h  "
              f"{ver:<12}{(f.window or 'always'):<38}{f.cadence_claimed}")
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
        verdict, sentence = staleness(channel, f)
        e_verdict, e_sentence = emptiness(items, 0)
        print(f"    HTTP {status}  {channel['format']}  "
              f"{len(items)} item(s)  conditional-GET headers: "
              f"{'yes' if got else 'NONE OFFERED'}")
        print(f"    title: {channel['title'][:68]}")
        print(f"    {verdict}: {sentence}")
        if e_verdict != "ok":
            print(f"    {e_verdict}: {e_sentence}")
            faults += 1
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
        verdict, sentence = staleness(channel, f)
        if verdict != "ok":
            notes.append(f"{f.key}: {verdict} - {sentence}")
        if channel.get("built"):
            st["last_build"] = channel["built"].isoformat()
        e_verdict, e_sentence = emptiness(items, int(st.get("max_items", 0)))
        if e_verdict != "ok":
            notes.append(f"{f.key}: {e_verdict} - {e_sentence}")
        st["max_items"] = max(int(st.get("max_items", 0)), len(items))

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
            t = turnover(len(fresh), len(items))
            if t:
                notes.append(f"{f.key}: {t}")
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
                in_scope, other = split_by_sic(fresh)
                for i in (in_scope if in_scope else fresh)[:12]:
                    when = (i["published"].date().isoformat()
                            if i["published"] else "undated")
                    tag = ""
                    if i["form"]:
                        # Multi-line expressions inside an f-string brace
                        # are a 3.12 feature; this runs on 3.11. Build
                        # the pieces first.
                        label = SPACE_SIC.get(i["sic"], "")
                        label = f" {label[:28]}" if label else ""
                        tag = (f"  [{i['form']} SIC "
                               f"{i['sic'] or '?'}{label}]")
                    print(f"    {when}  {i['title'][:62]}{tag}")
                if in_scope:
                    shown = min(len(in_scope), 12)
                    if len(in_scope) > shown:
                        print(f"    ... and {len(in_scope) - shown} more "
                              f"in scope")
                    print(f"    + {len(other)} filing(s) outside the "
                          f"in-scope SIC list" + _top_sics(other))
                elif len(fresh) > 12:
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


def cmd_check(c: Client, url: str) -> int:
    """Test ONE url that is not in the registry, and print the row it
    would deserve. Writes nothing.

    This exists because the registry can only grow honestly if adding a
    candidate is cheap. CelesTrak, ESA, arXiv and the FCC are all
    missing from feeds.tsv for the same reason: nobody had fetched them,
    and writing an unfetched URL into a registry is AD-112. `--check`
    makes the gap between "somebody mentioned a feed" and "we measured
    it" one command long, and it refuses the same things --verify
    refuses: a disallowed path, a 200 that is not a feed.
    """
    if not url.startswith("https://"):
        raise SystemExit("https only. A feed read over http can be "
                         "rewritten in transit.")
    print(f"\nCHECK (nothing is written)\n\n  {url}")
    allowed, why = c.robots(url)
    if not allowed:
        print(f"    REFUSED: {why}")
        return 1
    try:
        r = c.s.get(url, timeout=30)
        c._spend()
    except requests.RequestException as e:
        print(f"    FAILED: {e.__class__.__name__}: {e}")
        return 1
    ctype = r.headers.get("Content-Type", "?")
    cond = [h for h in ("ETag", "Last-Modified") if r.headers.get(h)]
    print(f"    HTTP {r.status_code}  {ctype!r}  {len(r.content)} bytes")
    print(f"    conditional GET: {', '.join(cond) if cond else 'NONE OFFERED'}")
    try:
        channel, items = parse_feed(r.text)
    except (ET.ParseError, ValueError) as e:
        print(f"    NOT A FEED: {e}")
        print(f"    Run --raw after adding it, or look at what it served "
              f"above. A 200 is not a feed.")
        return 1
    built = channel.get("built")
    print(f"    {channel['format']}, {len(items)} item(s), "
          f"title: {channel['title'][:60]}")
    print(f"    lastBuildDate: "
          f"{built.isoformat() if built else 'ABSENT - uncheckable for staleness'}")
    dated = [i["published"] for i in items if i["published"]]
    if len(dated) > 1:
        gaps = sorted((a - b).total_seconds() / 3600
                      for a, b in zip(dated, dated[1:]) if a > b)
        if gaps:
            print(f"    item spacing: median {gaps[len(gaps) // 2]:.1f}h "
                  f"across {len(dated)} dated items - a starting point for "
                  f"stale_after_hours, not an answer")
    print("\n  A registry row for it would need: cap, the publisher's "
          "stated\n  cadence (read it, do not infer it from the spacing "
          "above), a\n  stale_after_hours YOU choose, an operating window "
          "if one is\n  published, and a basis over 40 characters.")
    return 0


def cmd_raw(feeds: "list[Feed]", c: Client, key: str) -> int:
    """Show what one feed actually served. For the case --verify names
    but cannot explain: HTTP 200, bytes on the wire, and not a feed.

    fed_register_sec hit exactly that on 2026-10-13 - 200, 10,595 bytes,
    "not well-formed (invalid token): line 16, column 67" - and the URL
    pattern came from sec.gov's own RSS page, so the question of whether
    the SEC is advertising a pattern that no longer works is worth one
    request to settle. A tool that reports a fault should be able to
    show its evidence without anybody writing throwaway python.
    """
    f = next((x for x in feeds if x.key == key), None)
    if f is None:
        raise SystemExit(f"no feed named {key!r}. --list shows them.")
    allowed, why = c.robots(f.url)
    if not allowed:
        print(f"  REFUSED: {why}")
        return 1
    c._spend()                    # a diagnostic request is still a request
    c._wait(f.host)
    self_ = c.s.get(f.url, timeout=30)
    print(f"\n{f.key}  {f.url}")
    print(f"  HTTP {self_.status_code}  "
          f"Content-Type: {self_.headers.get('Content-Type')!r}")
    print(f"  {len(self_.content)} bytes on the wire, "
          f"{len(self_.text)} characters decoded")
    try:
        channel, items = parse_feed(self_.text)
        print(f"  PARSES: {channel['format']}, {len(items)} item(s)")
    except (ET.ParseError, ValueError) as e:
        print(f"  DOES NOT PARSE: {e}")
    print("\n  --- first 25 lines as served ---")
    for n, line in enumerate(self_.text.splitlines()[:25], start=1):
        print(f"  {n:>3} | {line[:150]}")
    return 0


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
    g.add_argument("--check", metavar="URL",
                   help="test a URL that is not in the registry and "
                        "print what a row for it would need. Writes "
                        "nothing")
    g.add_argument("--raw", metavar="KEY",
                   help="dump what one feed actually serves, for "
                        "diagnosing a 200 that is not a feed")
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
    if a.check:
        return cmd_check(c, a.check)
    if a.raw:
        return cmd_raw(feeds, c, a.raw)
    if a.verify:
        return 1 if cmd_verify(feeds, c, a.only, a.apply) else 0
    cmd_poll(feeds, c, a.include_unverified, not a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
