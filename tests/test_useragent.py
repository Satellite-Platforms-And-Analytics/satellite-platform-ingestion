"""
Every HTTP client this project ships must say who it is.

Why this exists
---------------
2026-09-14: the TechPort survey was blocked for a day by a 403 that was
never about the API key. api.nasa.gov's WAF refuses
`python-requests/<version>` before api.data.gov looks at the key at all.
One header changed it from 403 to 200 and 19,690 projects.

The header is therefore not politeness with a nice side effect - it is
load-bearing, and a refactor that drops it brings the outage back in a
form that looks like an authentication problem. Hence a test rather than
a comment.

The second assertion is the one with teeth: the User-Agent must not
contain browser tokens. `check_techport_403.py` measured that a Chrome
User-Agent also works, and declined to ship it - a client claiming to be
a browser is a claim that a person is at a keyboard, and this project
committed to not making claims like that to the sources it reads. The
test makes that a property of the code rather than a resolution.

Offline: no request is made, only the header the client would send.
"""
from __future__ import annotations

import importlib

import pytest

from src.useragent import PROJECT, VERSION, user_agent

#: Tokens that only appear when a client is pretending to be a browser.
BROWSER_TOKENS = ("mozilla", "applewebkit", "chrome", "safari", "gecko",
                  "edge", "firefox", "webkit")


def test_names_the_project_and_version() -> None:
    ua = user_agent("catalogue enrichment")
    assert ua.startswith(f"{PROJECT}/{VERSION}")


def test_states_purpose_and_rate() -> None:
    ua = user_agent("technology survey", "<=1000 req/hr")
    assert "technology survey" in ua
    assert "<=1000 req/hr" in ua


def test_contact_is_included_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTACT_EMAIL", "someone@example.com")
    assert "contact someone@example.com" in user_agent("survey")


def test_contact_is_omitted_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The address is the user's to publish, not the repository's. An unset
    CONTACT_EMAIL must not leave an empty `contact ` fragment behind.
    """
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    assert "contact" not in user_agent("survey")


def test_blank_contact_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTACT_EMAIL", "   ")
    assert "contact" not in user_agent("survey")


def test_techport_client_sends_a_descriptive_user_agent() -> None:
    """
    The regression that cost 2026-09-14. If this fails, the survey will
    come back 403 with an Apache HTML body that reads like an auth
    failure and is not one.
    """
    techport = importlib.import_module("check_techport")
    client = techport.Client("a" * 40)
    ua = client.session.headers.get("User-Agent", "")
    assert ua.startswith(PROJECT), (
        "the TechPort client is not identifying itself; api.nasa.gov's "
        "WAF will refuse it before the key is ever checked"
    )
    assert "req/hr" in ua, "the rate claim should be stated"


def test_no_client_impersonates_a_browser() -> None:
    techport = importlib.import_module("check_techport")
    ua = techport.Client("a" * 40).session.headers.get("User-Agent", "").lower()
    found = [t for t in BROWSER_TOKENS if t in ua]
    assert not found, (
        f"User-Agent contains browser tokens {found}. A Chrome string "
        f"also gets a 200 from this WAF; that is not a reason to send "
        f"one. See check_techport_403.py and AD-060."
    )


def test_builder_never_produces_a_browser_string() -> None:
    ua = user_agent("anything at all", "<=1 req/s").lower()
    assert not [t for t in BROWSER_TOKENS if t in ua]
