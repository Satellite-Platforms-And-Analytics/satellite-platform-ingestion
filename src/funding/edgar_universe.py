"""
Find every SEC filer in scope, from the submissions bulk, offline.

What this reads
---------------
`submissions.zip` from EDGAR, extracted: one `CIK##########.json` per
filer, 988,520 of them, carrying `cik`, `name`, `sic`, `sicDescription`,
`tickers`, `exchanges`, `stateOfIncorporation`, `formerNames` and the
whole filing index. Downloaded 2026-10-14 to D:\\Projects\\Data\\SEC_EDGAR.

Why this exists
---------------
`data/seed/sec_ciks.tsv` holds 16 tickers I chose by hand. That was
honest for a first pass and it is not a universe: it can only contain
companies somebody already thought of, which is the opposite of
discovery. The SEC assigns every filer an SIC code, the bulk carries it,
and `src/sic.py` holds the nine codes read from the SEC's own list - so
the universe can be *found* rather than recalled.

It also takes discovery offline. No rate limit, no robots.txt, no
User-Agent, no CONTACT_EMAIL gate: these are file reads. The API usage
policy still governs live calls in `sec_edgar.py`; it has nothing to say
about this module, and that is the point.

Three things measured before writing a line of it
-------------------------------------------------
1. **`sic` is NOT in the first 4KB of every file.** Measured on 300
   records: found in the first 4KB of 238, so a cheap head-read scanner
   would miss **21%** - and the misses would look like "this filer has
   no SIC" rather than "I did not read far enough". That is the 3761
   failure wearing a different hat (AD-112): an absent result that
   reads as a fact about the world. So the reader GROWS its window
   until it finds the key or runs out of file, and the two outcomes are
   counted separately.

2. **A real filer can have `sic: "0000"`.** CIK 3, a municipal
   investment trust, does. "0000" is a value the SEC assigned; "not
   found" is a failure of this scanner. Collapsing them would hide the
   second inside the first.

3. **79 files/sec through the device bridge mount**, which projects to
   ~3.5 hours. That is a measurement of the MOUNT, not of this machine
   running natively, and the two are not comparable. The run is
   therefore checkpointed and resumable rather than assumed to finish.

Two passes per file, not one
----------------------------
Pass 1 is a byte-level regex for `"sic":"NNNN"` on a growing read -
cheap, and run on all 988k. Pass 2 is a full `json.load` for the
handful that match, to pull names, tickers and filing history. Parsing
a million JSON documents to answer a question that a regex answers is
the kind of thoroughness that never finishes.

What it does NOT do
-------------------
No database writes. No network. No promotion into `data/seed/`: the
output is an artifact under `<bulk>/derived/`, and moving a row into
the seed stays a human act with a written basis, exactly as it is for
the 16 hand-picked ones. A scan that promoted its own findings would
turn "the SEC says this filer is SIC 3760" into "this project claims
this is a space company", and those are different sentences.
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
from datetime import date

_root = str(pathlib.Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)

from src.sic import SPACE_SIC                                 # noqa: E402

#: Default location of the extracted submissions bulk. Overridable with
#: --bulk-dir; same convention as src/resources.py's D:/ defaults.
#:
#: Points at the SUBDIRECTORY, not the dataset root. On 2026-10-14 both
#: zips had been extracted into one folder and silently overwrote each
#: other (see classify()); they were re-extracted into submissions/ and
#: companyfacts/ on 10-17 and verified clean - 401 of 401 sampled
#: records in submissions/ are submissions-shaped. precheck() below is
#: the control that keeps it that way.
DEFAULT_BULK = pathlib.Path(r"D:/Projects/Data/SEC_EDGAR/submissions")

#: Refuse to scan a directory where more than this share of sampled
#: records are the wrong dataset. Not zero, because companyfacts ships
#: a few empty {} documents and a stray file is not a merged directory.
MIXED_LIMIT = 0.01

#: Read this much first, then double until the key is found or the file
#: ends. 4KB catches ~79% (measured); the growth handles the rest
#: without reading whole files for the common case.
FIRST_READ = 4096
MAX_READ = 4 * 1024 * 1024

SIC_RE = re.compile(rb'"sic"\s*:\s*"(\d{0,4})"')

#: A companyfacts record, which is a DIFFERENT dataset wearing the same
#: filename. See classify() and the note on the collision below.
FACTS_RE = re.compile(rb'"entityName"\s*:')

#: Continuation files hold overflow filing history for filers with long
#: ones. They carry no `sic` and are not filer records; counting them
#: would inflate the denominator by 5,394.
CONTINUATION = "-submissions-"


def classify(path: str) -> "tuple[str, str|None]":
    """(kind, sic). kind is submissions | companyfacts | unknown.

    THE COLLISION THIS EXISTS TO CATCH
    ----------------------------------
    `submissions.zip` and `companyfacts.zip` both name their members
    `CIK##########.json`. Extracted into one directory, the second
    silently overwrites the first for every CIK present in both. That
    is what happened to D:/Projects/Data/SEC_EDGAR, measured
    2026-10-14 on an evenly spaced sample of 1,202 of the 988,520
    records:

        submissions-shaped   98.3%   ~971,249   has `sic`, `filings`
        companyfacts-shaped   1.7%    ~17,270   has `entityName`,
                                                `facts`, and NO `sic`

    1.7% sounds survivable and is not, because the overwritten CIKs are
    not a random 17,270. companyfacts only contains filers that publish
    XBRL financial statements - operating companies with real numbers,
    which is precisely the population a space-filer scan is looking
    for. Lockheed, Northrop, RTX and Boeing are all in that 1.7%, and
    for every one of them the record carrying the SIC is gone.

    The first version of this scanner counted them as "SIC NOT FOUND -
    a failure of THIS scanner". They are not: the reader worked
    perfectly and read a document of a different shape. A counter that
    asserts a cause it has not established is the error this project
    has now made four times (AD-105, AD-112, AD-113, AD-116), so the
    three outcomes are now measured apart and named.
    """
    want = FIRST_READ
    with open(path, "rb") as fh:
        buf = fh.read(want)
        while True:
            m = SIC_RE.search(buf)
            if m:
                return "submissions", m.group(1).decode()
            if FACTS_RE.search(buf):
                return "companyfacts", None
            if len(buf) < want or want >= MAX_READ:
                return _exhausted(buf)
            want = min(want * 2, MAX_READ)
            more = fh.read(want - len(buf))
            if not more:
                return _exhausted(buf)
            buf += more


def _exhausted(buf: bytes) -> "tuple[str, None]":
    """The whole file was read and matched neither shape.

    Measured 2026-10-14: 3 of the first 3,000 records are the two bytes
    `{}`. An empty document is not a scanner failure, and calling it one
    was the same mistake as calling a companyfacts record one, made
    again twenty minutes later in the same function. `unknown` now means
    only what it says.
    """
    if buf.strip() in (b"", b"{}", b"[]", b"null"):
        return "empty", None
    return "unknown", None


def find_sic(path: str) -> "str|None":
    """The filer's SIC, or None. Thin wrapper over classify()."""
    return classify(path)[1]


def filer_detail(path: str) -> dict:
    """Full parse, for in-scope filers only."""
    d = json.load(open(path, encoding="utf-8"))
    rec = (d.get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    dates = rec.get("filingDate") or []
    tenk = [dt for f, dt in zip(forms, dates) if str(f).startswith("10-K")]
    return {
        "cik": str(d.get("cik") or "").lstrip("0") or "0",
        "name": (d.get("name") or "").strip(),
        "sic": str(d.get("sic") or ""),
        "sic_description": (d.get("sicDescription") or "").strip(),
        "tickers": ",".join(d.get("tickers") or []),
        "exchanges": ",".join(d.get("exchanges") or []),
        "state": (d.get("stateOfIncorporation") or "").strip(),
        "entity_type": (d.get("entityType") or "").strip(),
        "former_names": " | ".join(
            (n.get("name") or "").strip()
            for n in (d.get("formerNames") or []))[:300],
        "filings_recent": str(len(forms)),
        "tenk_count": str(len(tenk)),
        "latest_10k": max(tenk) if tenk else "",
        "latest_filing": max(dates) if dates else "",
    }


COLUMNS = ["cik", "name", "sic", "sic_description", "tickers", "exchanges",
           "state", "entity_type", "tenk_count", "latest_10k",
           "latest_filing", "filings_recent", "former_names"]


def build_index(bulk: pathlib.Path, derived: pathlib.Path) -> pathlib.Path:
    """One sorted list of filer filenames, written once.

    scandir order is arbitrary and not stable between runs, so resuming
    by index would resume somewhere else. Sorting once costs a minute
    and makes both the resume and the run reproducible.
    """
    idx = derived / "filer_index.txt"
    stamp = derived / "filer_index.source.txt"
    want = str(bulk.resolve())
    if idx.is_file():
        have = stamp.read_text(encoding="utf-8").strip() \
            if stamp.is_file() else ""
        if have == want:
            return idx
        # Caught on 2026-10-17: --bulk-dir was repointed from the old
        # merged directory to submissions/, and build_index returned
        # the CACHED list because the file existed. The two happened to
        # hold the same CIKs, so it would have produced a correct
        # answer for the wrong reason - and the next time the paths
        # diverged it would have scanned one directory using another
        # one's file list, silently. A cache that cannot say what it is
        # a cache OF is not a cache.
        print(f"  index was built from {have or 'an unrecorded path'};")
        print(f"  this run wants {want} - rebuilding")
    else:
        print("  building the file index (once; scandir order is not "
              "stable)")
    names = []
    with os.scandir(bulk) as it:
        for e in it:
            n = e.name
            if n.startswith("CIK") and n.endswith(".json") \
                    and CONTINUATION not in n:
                names.append(n)
    names.sort()
    idx.write_text("\n".join(names) + "\n", encoding="utf-8")
    stamp.write_text(want + "\n", encoding="utf-8")
    print(f"  {len(names):,} filer records indexed")
    return idx


def precheck(bulk: pathlib.Path, names: "list[str]") -> None:
    """Refuse to scan a directory holding two datasets.

    This is the control for the 2026-10-14 collision. Both zips name
    their members CIK##########.json, so extracting them into one
    folder destroys one copy - and the scan's output then looks like a
    universe rather than a universe with a biased hole in it. Nothing
    detected that at extraction time; a sample at startup costs 200
    file reads and would have.
    """
    sample = names[::max(1, len(names) // 200)][:200]
    if not sample:
        return
    facts = sum(1 for n in sample if classify(str(bulk / n))[0]
                == "companyfacts")
    share = facts / len(sample)
    if share > MIXED_LIMIT:
        raise SystemExit(
            f"\n{bulk}\n"
            f"  {facts} of {len(sample)} sampled records ({share:.1%}) "
            f"are companyfacts documents,\n"
            f"  not submissions records. These are two datasets whose "
            f"files share the name\n"
            f"  CIK##########.json, so extracting both into one "
            f"directory overwrites one.\n\n"
            f"  Extract each zip into its OWN subdirectory and point "
            f"--bulk-dir at the\n"
            f"  submissions one. Refusing rather than reporting a "
            f"universe with a hole in\n"
            f"  it that is biased towards exactly the filers this scan "
            f"is looking for.")


def main(argv: "list[str]|None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.funding.edgar_universe",
        description="Scan the EDGAR submissions bulk for in-scope SIC "
                    "codes. Offline. Writes an artifact, never the seed.")
    p.add_argument("--bulk-dir", type=pathlib.Path, default=DEFAULT_BULK,
                   help="the extracted SUBMISSIONS directory")
    p.add_argument("--out-dir", type=pathlib.Path, default=None,
                   help="where the artifact goes. Defaults to a "
                        "derived/ beside the dataset folders, so it "
                        "never lands inside the extracted data")
    p.add_argument("--skip-precheck", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--limit", type=int, default=0,
                   help="stop after N records; for a smoke test")
    p.add_argument("--restart", action="store_true",
                   help="ignore the checkpoint and start over")
    a = p.parse_args(argv)

    bulk = a.bulk_dir
    if not bulk.is_dir():
        raise SystemExit(f"no such directory: {bulk}\nPass --bulk-dir.")
    # derived/ sits beside the dataset folders, not inside one: the
    # extracted trees should stay exactly as the zips left them, so a
    # re-extract never has to step around our output.
    derived = a.out_dir or (
        bulk.parent / "derived"
        if bulk.name in ("submissions", "companyfacts")
        else bulk / "derived")
    derived.mkdir(parents=True, exist_ok=True)
    state_f = derived / "scan_state.json"
    out_f = derived / f"space_filers_{date.today().isoformat()}.tsv"

    idx = build_index(bulk, derived)
    names = idx.read_text(encoding="utf-8").split("\n")
    names = [n for n in names if n]
    total = len(names)
    if not a.skip_precheck:
        precheck(bulk, names)

    state = {"next": 0, "found": 0, "sic_blank": 0, "companyfacts": 0,
             "empty": 0, "unknown": 0, "by_sic": {}}
    if state_f.is_file() and not a.restart:
        try:
            state.update(json.loads(state_f.read_text(encoding="utf-8")))
            print(f"  resuming at {state['next']:,} of {total:,}")
        except (OSError, ValueError):
            print("  checkpoint unreadable; starting over")

    start = state["next"]
    stop = min(total, start + a.limit) if a.limit else total
    if start == 0:
        out_f.write_text("\t".join(COLUMNS) + "\n", encoding="utf-8")

    hits, t0, last_save = [], time.time(), time.time()
    for i in range(start, stop):
        path = str(bulk / names[i])
        try:
            kind, sic = classify(path)
        except OSError:
            kind, sic = "unknown", None
        if kind == "companyfacts":
            state["companyfacts"] += 1
        elif kind == "empty":
            state["empty"] += 1
        elif kind == "unknown":
            state["unknown"] += 1
        elif sic in SPACE_SIC:
            state["found"] += 1
            state["by_sic"][sic] = state["by_sic"].get(sic, 0) + 1
            try:
                hits.append(filer_detail(path))
            except (OSError, ValueError) as e:
                print(f"  ! {names[i]} matched {sic} but would not parse: "
                      f"{e}")
        elif sic in ("", "0000"):
            state["sic_blank"] += 1

        done = i + 1
        if hits and (time.time() - last_save > 30 or done == stop):
            with open(out_f, "a", encoding="utf-8", newline="\n") as fh:
                for h in hits:
                    fh.write("\t".join(h[c].replace("\t", " ")
                                       for c in COLUMNS) + "\n")
            hits = []
            state["next"] = done
            state_f.write_text(json.dumps(state, indent=1),
                               encoding="utf-8")
            last_save = time.time()
        if done % 20000 == 0:
            rate = (done - start) / max(time.time() - t0, 1e-9)
            left = (stop - done) / max(rate, 1e-9) / 60
            print(f"  {done:,}/{total:,}  {rate:,.0f}/s  "
                  f"{state['found']} in scope  ~{left:.0f} min left")

    if hits:
        with open(out_f, "a", encoding="utf-8", newline="\n") as fh:
            for h in hits:
                fh.write("\t".join(h[c].replace("\t", " ")
                                   for c in COLUMNS) + "\n")
    state["next"] = stop
    state_f.write_text(json.dumps(state, indent=1), encoding="utf-8")

    print(f"\n  scanned      {stop - start:,} records "
          f"({stop:,} of {total:,} overall)")
    print(f"  IN SCOPE     {state['found']:,}")
    for code, n in sorted(state["by_sic"].items(),
                          key=lambda kv: -kv[1]):
        print(f"      {code}  {n:>5}  {SPACE_SIC[code]}")
    print(f"  sic 0000/''  {state['sic_blank']:,}  "
          f"(a value the SEC assigned, not a failure)")
    print(f"  COMPANYFACTS {state['companyfacts']:,}  "
          f"(a DIFFERENT dataset under the same filename)")
    print(f"  empty        {state['empty']:,}  "
          f"(the two bytes {{}}; the SEC ships some that way)")
    print(f"  unreadable   {state['unknown']:,}  "
          f"(neither shape, not empty; a real failure of this scanner)")
    if state["companyfacts"]:
        pct = state["companyfacts"] / max(stop - start, 1)
        print(f"\n  ** THE UNIVERSE BELOW IS INCOMPLETE. **\n"
              f"  {state['companyfacts']:,} records ({pct:.1%}) are "
              f"companyfacts documents, which carry no SIC.\n"
              f"  submissions.zip and companyfacts.zip both name their "
              f"members CIK##########.json,\n"
              f"  so extracting both into one directory overwrote the "
              f"submissions record for\n"
              f"  every CIK in both. Those CIKs cannot be classified "
              f"and are missing here.\n"
              f"  They are not a random sample: companyfacts holds only "
              f"XBRL filers, i.e.\n"
              f"  operating companies with financial statements - the "
              f"exact population this\n"
              f"  scan is for. Re-extract each zip into its OWN "
              f"subdirectory, then re-run.")
    print(f"\n  artifact: {out_f}")
    print("  Nothing was promoted into data/seed/sec_ciks.tsv. Moving a "
          "row there is a\n  human act with a written basis - the SEC "
          "saying 'SIC 3760' and this project\n  claiming 'space "
          "company' are different sentences.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
