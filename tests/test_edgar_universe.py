"""
Tests for the EDGAR submissions-bulk universe scan.

Fixtures are shaped like the real records measured on 2026-10-14,
including the two cases that a cheap scanner gets wrong: a `sic` key
pushed past the first read by a long `addresses`/`formerNames` block,
and a real filer whose SIC is the string "0000".
"""
from __future__ import annotations

import json
import pathlib

import pytest

from src.funding import edgar_universe as eu


def _write(d: pathlib.Path, cik: int, sic: str, name: str,
           pad: int = 0, forms=None) -> pathlib.Path:
    """A submissions record. `pad` pushes `sic` further into the file,
    the way a long addresses/formerNames block does in the real data."""
    rec = {
        "cik": f"{cik:010d}",
        "addresses": {"business": {"street1": "x" * pad}},
        "formerNames": [{"name": "Old Name Inc"}] if pad else [],
        "name": name,
        "sic": sic,
        "sicDescription": "desc",
        "tickers": ["TKR"],
        "exchanges": ["NASDAQ"],
        "stateOfIncorporation": "DE",
        "entityType": "operating",
        "filings": {"recent": {
            "form": forms or ["10-K", "8-K"],
            "filingDate": ["2026-02-01", "2026-03-01"][:len(forms or [1, 2])],
        }, "files": []},
    }
    p = d / f"CIK{cik:010d}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_sic_is_found_even_when_it_sits_past_the_first_read(tmp_path):
    """Measured on the real bulk: `sic` is inside the first 4KB for only
    238 of 300 records. A head-read scanner would miss 21% of the
    universe AND report the misses as "this filer has no SIC" — an
    absent result reading as a fact about the world, which is AD-112."""
    p = _write(tmp_path, 1750, "3760", "Deep Filer", pad=50_000)
    assert p.stat().st_size > eu.FIRST_READ * 10
    assert eu.find_sic(str(p)) == "3760"


def test_a_real_sic_of_0000_is_a_value_not_a_failure(tmp_path):
    """CIK 3 — a municipal investment trust — really does carry
    sic "0000". Collapsing that into "not found" would hide a scanner
    failure inside a data value."""
    p = _write(tmp_path, 3, "0000", "Defined Asset Funds")
    assert eu.find_sic(str(p)) == "0000"


def test_a_file_with_no_sic_key_returns_none(tmp_path):
    p = tmp_path / "CIK0000009999.json"
    p.write_text(json.dumps({"cik": "0000009999", "name": "No SIC Co"}),
                 encoding="utf-8")
    assert eu.find_sic(str(p)) is None
    assert eu.classify(str(p))[0] == "unknown"


def test_an_empty_record_is_empty_not_unreadable(tmp_path):
    """Measured: 3 of the first 3,000 records are the two bytes `{}`.
    An empty document is not a scanner failure, and calling it one was
    the same mistake as calling a companyfacts record one - made again
    twenty minutes later in the same function."""
    p = tmp_path / "CIK0000003521.json"
    p.write_text("{}", encoding="utf-8")
    assert eu.classify(str(p)) == ("empty", None)


def test_the_index_skips_continuation_files(tmp_path):
    """5,394 of the 993,915 files are paginated filing history, not
    filer records. They carry no `sic`; counting them would inflate the
    denominator and manufacture 5,394 'SIC NOT FOUND' failures."""
    _write(tmp_path, 1800, "3760", "Real Filer")
    (tmp_path / "CIK0000001800-submissions-001.json").write_text(
        '{"form":[]}', encoding="utf-8")
    (tmp_path / "placeholder.txt").write_text("x", encoding="utf-8")
    derived = tmp_path / "derived"
    derived.mkdir()
    idx = eu.build_index(tmp_path, derived)
    names = [n for n in idx.read_text(encoding="utf-8").split("\n") if n]
    assert names == ["CIK0000001800.json"]


def test_detail_extracts_what_the_seed_needs(tmp_path):
    p = _write(tmp_path, 936468, "3760", "Some Space Co",
               forms=["10-K", "10-K"])
    d = eu.filer_detail(str(p))
    assert d["cik"] == "936468"          # leading zeros stripped for joins
    assert d["sic"] == "3760"
    assert d["tickers"] == "TKR"
    assert d["tenk_count"] == "2"
    assert d["latest_10k"] == "2026-03-01"


def test_the_scan_writes_an_artifact_and_never_the_seed(tmp_path, capsys):
    """The output is a regenerable artifact under <bulk>/derived/.
    Promotion into data/seed/sec_ciks.tsv stays a human act with a
    written basis: "the SEC says SIC 3760" and "this project claims
    space company" are different sentences."""
    _write(tmp_path, 1, "3760", "In Scope Co")
    _write(tmp_path, 2, "2834", "Pharma Co")
    _write(tmp_path, 3, "0000", "Trust")
    seed_before = eu.pathlib.Path(
        eu.__file__).resolve().parents[2] / "data" / "seed" / "sec_ciks.tsv"
    before = seed_before.read_bytes() if seed_before.is_file() else None

    assert eu.main(["--bulk-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "IN SCOPE     1" in out
    assert "COMPANYFACTS 0" in out

    rows = [l for l in (tmp_path / "derived").glob("space_filers_*.tsv")]
    assert len(rows) == 1
    lines = rows[0].read_text(encoding="utf-8").strip().split("\n")
    assert lines[0].split("\t") == eu.COLUMNS
    assert len(lines) == 2 and "In Scope Co" in lines[1]
    if before is not None:
        assert seed_before.read_bytes() == before, "the seed was touched"


def test_the_scanner_is_offline_and_touches_no_database():
    """Discovery is file reads now: no rate limit, no robots, no
    User-Agent, no CONTACT_EMAIL gate. If an import of requests or the
    DB layer ever appears here, that claim quietly stops being true."""
    import tokenize
    with tokenize.open(eu.__file__) as fh:
        code = " ".join(
            t.string for t in tokenize.generate_tokens(fh.readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING))
    for forbidden in ("requests", "urllib", "httpx", "socket",
                      "src.db", "sqlalchemy", "DATABASE_URL",
                      "sec_edgar"):
        assert forbidden not in code, f"{forbidden!r} in edgar_universe"


def test_it_reads_the_one_shared_sic_definition():
    from src.sic import SPACE_SIC
    assert eu.SPACE_SIC is SPACE_SIC
    assert "3760" in eu.SPACE_SIC and "3761" not in eu.SPACE_SIC


def test_a_companyfacts_record_is_named_not_counted_as_a_failure(tmp_path):
    """submissions.zip and companyfacts.zip both name their members
    CIK##########.json. Extracted into one directory the second
    overwrites the first, which is what happened to the real download:
    ~17,270 of 988,520 records (1.7%) are companyfacts documents.

    The first version of the scanner called these "SIC NOT FOUND - a
    failure of THIS scanner". The reader worked perfectly and read a
    document of a different shape. Four times now this project has had
    a counter assert a cause it had not established."""
    p = tmp_path / "CIK0000001750.json"
    p.write_text(json.dumps({"cik": "0000001750",
                             "entityName": "AAR CORP",
                             "facts": {"us-gaap": {}}}), encoding="utf-8")
    kind, sic = eu.classify(str(p))
    assert kind == "companyfacts" and sic is None


def test_the_scan_refuses_to_call_an_incomplete_universe_complete(
        tmp_path, capsys):
    """1.7% sounds survivable and is not: companyfacts holds only XBRL
    filers, so the overwritten CIKs are the operating companies with
    real financial statements - exactly the population being scanned
    for. The run must say so rather than printing a tidy total."""
    _write(tmp_path, 1, "3760", "In Scope Co")
    (tmp_path / "CIK0000001750.json").write_text(
        json.dumps({"cik": "1750", "entityName": "AAR", "facts": {}}),
        encoding="utf-8")
    # --skip-precheck because precheck() now refuses a directory this
    # badly mixed before the scan starts. The two controls cover
    # different cases: precheck stops an obviously merged directory,
    # this warning catches contamination that slipped under its
    # threshold. Exercising the second one means getting past the
    # first.
    assert eu.main(["--bulk-dir", str(tmp_path), "--restart",
                    "--skip-precheck"]) == 0
    out = capsys.readouterr().out
    assert "THE UNIVERSE BELOW IS INCOMPLETE" in out
    assert "not a random sample" in out


def test_classify_still_reads_a_deep_sic(tmp_path):
    p = _write(tmp_path, 1750, "3760", "Deep Filer", pad=50_000)
    assert eu.classify(str(p)) == ("submissions", "3760")


# ── the control for the collision ────────────────────────────────────

def test_precheck_refuses_a_directory_holding_both_datasets(tmp_path):
    """The 2026-10-14 collision, turned into a guard. Nothing detected
    it at extraction time; a 200-record sample at startup costs
    nothing and would have."""
    for i in range(1, 30):
        _write(tmp_path, i, "3760", f"Filer {i}")
    for i in range(100, 110):          # ~25% companyfacts-shaped
        (tmp_path / f"CIK{i:010d}.json").write_text(
            json.dumps({"cik": str(i), "entityName": "X", "facts": {}}),
            encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        eu.main(["--bulk-dir", str(tmp_path), "--restart"])
    msg = str(e.value)
    assert "two datasets" in msg and "OWN subdirectory" in msg


def test_precheck_tolerates_a_stray_file(tmp_path):
    """Not zero: companyfacts ships a few empty {} documents and one
    stray file is not a merged directory. A guard that fires on noise
    gets switched off."""
    for i in range(1, 300):
        _write(tmp_path, i, "2834", f"Pharma {i}")
    (tmp_path / "CIK0000009001.json").write_text(
        json.dumps({"cik": "9001", "entityName": "X", "facts": {}}),
        encoding="utf-8")
    assert eu.main(["--bulk-dir", str(tmp_path), "--restart"]) == 0


def test_the_artifact_never_lands_inside_the_extracted_data(tmp_path):
    """The extracted trees stay exactly as the zips left them, so a
    re-extract never has to step around our output."""
    bulk = tmp_path / "submissions"
    bulk.mkdir()
    _write(bulk, 1, "3760", "In Scope Co")
    assert eu.main(["--bulk-dir", str(bulk), "--restart"]) == 0
    assert not (bulk / "derived").exists()
    assert list((tmp_path / "derived").glob("space_filers_*.tsv"))


def test_the_index_is_rebuilt_when_the_bulk_directory_changes(tmp_path):
    """Caught live on 2026-10-17: --bulk-dir was repointed from the
    merged directory to submissions/ and build_index returned the
    CACHED list, because the file existed. Both happened to hold the
    same CIKs, so it would have been right for the wrong reason — and
    the next time the paths diverged it would have scanned one
    directory using another's file list, silently. A cache that cannot
    say what it is a cache of is not a cache."""
    a, b = tmp_path / "a", tmp_path / "b"
    derived = tmp_path / "derived"
    a.mkdir(); b.mkdir(); derived.mkdir()
    _write(a, 1, "3760", "In A")
    _write(b, 2, "3760", "In B")
    _write(b, 3, "3760", "Also In B")

    eu.build_index(a, derived)
    assert (derived / "filer_index.txt").read_text().split() == \
        ["CIK0000000001.json"]

    eu.build_index(b, derived)
    got = (derived / "filer_index.txt").read_text().split()
    assert got == ["CIK0000000002.json", "CIK0000000003.json"], got
    assert (derived / "filer_index.source.txt").read_text().strip() == \
        str(b.resolve())
