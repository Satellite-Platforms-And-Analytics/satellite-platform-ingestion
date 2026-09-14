"""
The NASA key is validated locally, before a request is spent on it.

Why this exists
---------------
On 2026-09-12 the TechPort survey came back `403 Client Error: Forbidden`
and nothing else. Two fixes followed. The first made the server's own
explanation visible, because `raise_for_status()` had been throwing the
response body away. This is the second: a 403 that says API_KEY_INVALID
is a slow and lossy way to be told something the client could have
checked for free.

The padded-key case is the one worth naming. A key pasted into `.env`
with a trailing space or a stray CR is a 40-character key to a human
reading the file and a 41-character key to api.data.gov, which answers
403 API_KEY_INVALID - an error whose wording sends you looking for a
*wrong* key when the key is merely *padded*. `_key()` strips, so that
particular hour does not get spent.

Nothing here makes a network call, and no test may ever print a key.
"""
from __future__ import annotations

import importlib

import pytest

techport = importlib.import_module("check_techport")

GOOD = "a" * 20 + "B" * 10 + "9" * 10          # 40 chars, alphanumeric


def test_good_key_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NASA_API_KEY", GOOD)
    assert techport._key() == GOOD


def test_surrounding_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 403 that reads as 'wrong key' and means 'padded key'."""
    monkeypatch.setenv("NASA_API_KEY", f"  {GOOD}\r\n")
    assert techport._key() == GOOD


def test_demo_key_is_allowed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    DEMO_KEY is not 40 characters and is documented as valid. It rate
    limits itself at 30/hour, so the survey still cannot run on it - but
    the path can be exercised without registering.
    """
    monkeypatch.setenv("NASA_API_KEY", "DEMO_KEY")
    assert techport._key() == "DEMO_KEY"


def test_missing_key_fails_with_where_to_get_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NASA_API_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        techport._key()
    assert "api.nasa.gov" in str(e.value)


def test_whitespace_only_key_is_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NASA_API_KEY", "   ")
    with pytest.raises(SystemExit) as e:
        techport._key()
    assert "is not set" in str(e.value)


@pytest.mark.parametrize(
    "bad,why",
    [
        ("a" * 32, "truncated paste"),
        ("a" * 41, "one character too many"),
        ("a" * 39 + "!", "punctuation"),
        ('"' + "a" * 38 + '"', "quotes came through from .env"),
        ("a" * 20 + " " + "b" * 19, "embedded space"),
    ],
)
def test_malformed_key_is_rejected_before_any_request(
    monkeypatch: pytest.MonkeyPatch, bad: str, why: str
) -> None:
    monkeypatch.setenv("NASA_API_KEY", bad)
    with pytest.raises(SystemExit) as e:
        techport._key()
    assert "shape" in str(e.value), why


def test_rejection_message_never_contains_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    This message is the kind of thing that ends up in a screenshot or a
    paste. It may report the length; it may not report the value.
    """
    secret = "s3cr3t" * 6 + "XY"                # 38 chars, malformed
    monkeypatch.setenv("NASA_API_KEY", secret)
    with pytest.raises(SystemExit) as e:
        techport._key()
    msg = str(e.value)
    assert secret not in msg
    assert "s3cr3t" not in msg
    assert "38" in msg
