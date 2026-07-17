"""Spreading a queue of scans across the pool, and pacing the request rate.

Both exist because of one production incident (2026-07-17): a queue of scans
drained a single account to 5000/5000 — collecting a scraping warning — while a
sibling token in the same pool had spent exactly one request; and the burst rate
earned a secondary throttle that refused even a brand-new, full-quota token.
"""

from __future__ import annotations

import httpx
import pytest

import scanner.github as github
from scanner.github import GitHubClient


@pytest.fixture(autouse=True)
def clean_pool_state(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    monkeypatch.chdir(tmp_path)
    github._token_cooldowns.clear()
    github._token_remaining.clear()
    github.min_request_interval_seconds = 0.0
    yield
    github._token_cooldowns.clear()
    github._token_remaining.clear()
    github.min_request_interval_seconds = 0.0


POOL = ["tok-a", "tok-b", "tok-c"]


def _client(remaining: str | None = "4999", status: int = 200):
    """A client over the 3-token pool whose responses report `remaining`."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {} if remaining is None else {"x-ratelimit-remaining": remaining}
        return httpx.Response(status, json={"ok": True}, headers=headers)

    return GitHubClient(token=POOL, transport=httpx.MockTransport(handler)), seen


def _auth(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


# --- spread ---------------------------------------------------------------

def test_an_untried_token_is_preferred_over_a_spent_one():
    github._token_remaining["tok-a"] = 10
    client, seen = _client()
    client.get("/x")
    # tok-b has never been called, so it is assumed full and outranks tok-a's 10.
    assert _auth(seen[0]) == "tok-b"


def test_the_roomiest_token_wins():
    github._token_remaining.update({"tok-a": 10, "tok-b": 20, "tok-c": 4000})
    client, seen = _client()
    client.get("/x")
    assert _auth(seen[0]) == "tok-c"


def test_a_fresh_pool_still_starts_at_the_first_slot():
    """Nothing known yet: every token ties, and pool order decides — so
    single-token and first-run behaviour is exactly as it was."""
    client, seen = _client()
    client.get("/x")
    assert _auth(seen[0]) == "tok-a"


def test_a_cooling_token_is_never_chosen_however_roomy_it_looks():
    github._token_remaining.update({"tok-a": 5000, "tok-b": 1, "tok-c": 1})
    github._token_cooldowns["tok-a"] = 2 ** 31  # far future
    client, seen = _client()
    client.get("/x")
    assert _auth(seen[0]) != "tok-a"


def test_headroom_is_learned_from_the_response_header():
    client, _ = _client(remaining="4321")
    client.get("/x")
    assert github._token_remaining["tok-a"] == 4321


def test_a_response_without_the_header_leaves_headroom_unknown():
    client, _ = _client(remaining=None)
    client.get("/x")
    assert "tok-a" not in github._token_remaining


def test_a_nonsense_header_is_ignored_rather_than_crashing():
    client, _ = _client(remaining="not-a-number")
    client.get("/x")
    assert "tok-a" not in github._token_remaining


def test_successive_scans_spread_across_the_pool():
    """The regression this exists for: each scan builds a new client, so
    'first usable token' meant every scan hammered slot 0."""
    chosen = []
    for _ in range(6):
        # Each client reports the chosen token a little more spent, exactly as
        # GitHub would.
        def handler(request: httpx.Request) -> httpx.Response:
            token = _auth(request)
            left = github._token_remaining.get(token, github.ASSUMED_HOURLY_LIMIT) - 1
            return httpx.Response(200, json={}, headers={"x-ratelimit-remaining": str(left)})

        client = GitHubClient(token=POOL, transport=httpx.MockTransport(handler))
        client.get("/x")
        chosen.append(client.token)

    # Every token pulls its weight instead of one being drained.
    assert set(chosen) == set(POOL)
    assert max(chosen.count(t) for t in POOL) - min(chosen.count(t) for t in POOL) <= 1


def test_unauthenticated_use_is_unaffected():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"ok": True})

    client = GitHubClient(token=[], transport=httpx.MockTransport(handler))
    assert client.get("/x") == {"ok": True}
    assert github._token_remaining == {}


# --- anonymous fallback ---------------------------------------------------
#
# GitHub's 2026-07-16 incident failed ~35% of *authenticated* REST requests
# while serving anonymous ones from the same host. Everything this client reads
# is public, so an anonymous answer is the same answer.

def _authed_5xx_then(anon_status: int, anon_body=None):
    """Transport where authenticated calls 503 and anonymous ones answer."""
    seen: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authed = "Authorization" in request.headers
        seen.append((str(request.url), authed))
        if authed:
            return httpx.Response(503, text="<html>Unicorn!</html>")
        return httpx.Response(anon_status, json=anon_body if anon_body is not None else {})

    return handler, seen


def test_an_authenticated_5xx_falls_back_to_anonymous(monkeypatch):
    handler, seen = _authed_5xx_then(200, {"ok": True})
    monkeypatch.setattr(GitHubClient, "_sleep", staticmethod(lambda _s: None))
    # The fallback builds its own client, so point that at the mock too.
    real_client = httpx.Client
    monkeypatch.setattr(
        github.httpx, "Client",
        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}),
    )

    client = GitHubClient(token=POOL, transport=httpx.MockTransport(handler))
    assert client.get("/repos/acme/demo") == {"ok": True}

    assert seen[0][1] is True    # tried authenticated first
    assert seen[1][1] is False   # then anonymously
    assert len(seen) == 2        # and stopped there, no waiting


def test_the_fallback_is_logged(monkeypatch, caplog):
    handler, _ = _authed_5xx_then(200, {"ok": True})
    monkeypatch.setattr(GitHubClient, "_sleep", staticmethod(lambda _s: None))
    real_client = httpx.Client
    monkeypatch.setattr(
        github.httpx, "Client",
        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}),
    )
    with caplog.at_level("WARNING", logger="scanner.github"):
        GitHubClient(token=POOL, transport=httpx.MockTransport(handler)).get("/x")
    assert any("served anonymously instead" in r.getMessage() for r in caplog.records)


def test_an_anonymous_rate_limit_is_not_mistaken_for_an_answer(monkeypatch):
    """403 there is the 60/hour IP budget; it must not be returned as data."""
    handler, seen = _authed_5xx_then(403, {"message": "rate limit"})
    naps: list[float] = []
    clock = [1000.0]

    def fake_sleep(s):
        naps.append(s)
        clock[0] += max(s, 0.0)

    monkeypatch.setattr(GitHubClient, "_sleep", staticmethod(fake_sleep))
    monkeypatch.setattr(github.time, "monotonic", lambda: clock[0])
    real_client = httpx.Client
    monkeypatch.setattr(
        github.httpx, "Client",
        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}),
    )

    client = GitHubClient(token=POOL, transport=httpx.MockTransport(handler))
    with pytest.raises(github.GitHubError):
        client.get("/x")
    assert naps  # fell back to waiting on the authenticated path


def test_unauthenticated_clients_do_not_retry_themselves_anonymously():
    """Already anonymous: the fallback would be the identical request."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = GitHubClient(token=[], transport=httpx.MockTransport(handler))
    assert client.get("/x") == {"ok": True}
    assert len(seen) == 1


# --- pace -----------------------------------------------------------------

def test_pacing_is_off_by_default_so_a_cli_scan_is_never_slowed(monkeypatch):
    naps: list[float] = []
    monkeypatch.setattr(GitHubClient, "_sleep", staticmethod(naps.append))
    client, _ = _client()
    client.get("/x")
    client.get("/y")
    assert naps == []


def test_pacing_holds_requests_apart_when_configured(monkeypatch):
    naps: list[float] = []
    monkeypatch.setattr(GitHubClient, "_sleep", staticmethod(naps.append))
    clock = [1000.0]
    monkeypatch.setattr(github.time, "monotonic", lambda: clock[0])
    github.min_request_interval_seconds = 2.0

    client, _ = _client()
    client.get("/x")   # first request: nothing to wait for
    client.get("/y")   # immediately after: must wait the full interval

    assert naps and naps[-1] == pytest.approx(2.0)


def test_the_interval_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_MIN_REQUEST_INTERVAL_SECONDS", "1.5")
    assert github._default_min_interval() == 1.5


@pytest.mark.parametrize("value", ["", "abc", "-1"])
def test_a_bad_or_absent_interval_means_no_pacing(monkeypatch, value):
    monkeypatch.setenv("GITHUB_MIN_REQUEST_INTERVAL_SECONDS", value)
    assert github._default_min_interval() == 0.0
