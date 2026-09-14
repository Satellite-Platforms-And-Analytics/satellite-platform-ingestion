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
