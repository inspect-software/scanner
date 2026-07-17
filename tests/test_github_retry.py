"""Centralized transient-failure policy in GitHubClient._send."""

from __future__ import annotations

import json
import time

import httpx
import pytest

import scanner.github as github_module
from scanner.github import GitHubClient, GitHubError, RepoNotFoundError


# `instant_sleep` is the suite-wide autouse fixture in conftest.py.


@pytest.fixture(autouse=True)
def clean_pool_state():
    """Both pool globals are process-wide; leaking either between tests would
    make token choice depend on test order."""
    github_module._token_cooldowns.clear()
    github_module._token_remaining.clear()
    github_module.min_request_interval_seconds = 0.0
    yield
    github_module._token_cooldowns.clear()
    github_module._token_remaining.clear()
    github_module.min_request_interval_seconds = 0.0


def make_client(responses, token="tok"):
    """Client whose transport pops canned responses; also returns the request log."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = responses[min(len(requests), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        status, body, headers = item
        return httpx.Response(status, json=body, headers=headers)

    client = GitHubClient(token=token, transport=httpx.MockTransport(handler))
    return client, requests


def test_transient_5xx_retries_until_success(instant_sleep):
    gh, requests = make_client([
        (502, {"message": "bad gateway"}, {}),
        (503, {"message": "unavailable"}, {}),
        (200, {"ok": True}, {}),
    ])
    assert gh.get("/repos/acme/demo") == {"ok": True}
    assert len(requests) == 3
    assert instant_sleep == [1.0, 2.0]  # exponential backoff


def test_network_errors_retry_then_raise(instant_sleep):
    gh, requests = make_client([httpx.ConnectError("boom")])
    with pytest.raises(GitHubError, match="Network error"):
        gh.get("/repos/acme/demo")
    # Retries are bounded by wall clock, not a count: it keeps trying until the
    # next backoff would overrun RETRY_BUDGET_SECONDS.
    assert sum(instant_sleep) <= github_module.RETRY_BUDGET_SECONDS
    assert len(requests) > 4  # more patient than the old 4-attempt cap


def test_secondary_rate_limit_honors_retry_after(instant_sleep):
    gh, requests = make_client([
        (403, {"message": "abuse"}, {"retry-after": "7", "x-ratelimit-remaining": "42"}),
        (200, {"ok": True}, {}),
    ])
    assert gh.get("/anything") == {"ok": True}
    assert instant_sleep == [7.0]
    assert len(requests) == 2


def test_primary_exhaustion_rotates_to_next_token(instant_sleep):
    reset = str(int(time.time()) + 3600)
    gh, requests = make_client([
        (403, {"message": "limited"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
        (200, {"ok": True}, {}),
    ], token=["tok-a", "tok-b"])
    assert gh.get("/repos/acme/demo") == {"ok": True}
    assert requests[0].headers["authorization"] == "Bearer tok-a"
    assert requests[1].headers["authorization"] == "Bearer tok-b"
    assert instant_sleep == []  # rotation is free, no waiting


def test_pool_exhaustion_waits_for_a_near_reset(instant_sleep):
    reset = str(int(time.time()) + 30)  # within RATE_LIMIT_MAX_WAIT_SECONDS
    gh, requests = make_client([
        (403, {"message": "limited"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
        (200, {"ok": True}, {}),
    ])
    assert gh.get("/repos/acme/demo") == {"ok": True}
    assert len(instant_sleep) == 1 and 0 < instant_sleep[0] <= 32


def test_pool_exhaustion_with_distant_reset_fails_fast(instant_sleep):
    reset = str(int(time.time()) + 3600)  # far beyond the wait cap
    gh, _ = make_client([
        (403, {"message": "limited"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
    ])
    with pytest.raises(GitHubError, match="rate limit exceeded"):
        gh.get("/repos/acme/demo")
    assert instant_sleep == []


def test_graphql_returns_data_and_counts_requests():
    gh, requests = make_client([(200, {"data": {"repository": {"name": "demo"}}}, {})])
    assert gh.graphql("query {}", {}) == {"repository": {"name": "demo"}}
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/graphql"
    assert json.loads(requests[0].content)["variables"] == {}
    assert gh.requests_made == 1


def test_graphql_not_found_error_type():
    gh, _ = make_client([
        (200, {"data": None, "errors": [{"type": "NOT_FOUND", "message": "gone"}]}, {}),
    ])
    with pytest.raises(RepoNotFoundError):
        gh.graphql("query {}", {})


def test_graphql_requires_token():
    gh, _ = make_client([(200, {}, {})], token=None)
    gh.tokens = []
    gh.token = None
    with pytest.raises(GitHubError, match="requires an authentication token"):
        gh.graphql("query {}", {})


def test_every_silent_wait_leaves_a_log_line(instant_sleep, caplog):
    """Operators must see why a scan stalls: each retry/pause logs a warning."""
    reset = str(int(time.time()) + 30)
    with caplog.at_level("WARNING", logger="scanner.github"):
        gh, _ = make_client([
            (502, {"message": "bad gateway"}, {}),
            (403, {"message": "abuse"}, {"retry-after": "7", "x-ratelimit-remaining": "42"}),
            (403, {"message": "limited"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
            (200, {"ok": True}, {}),
        ])
        assert gh.get("/repos/acme/demo") == {"ok": True}
    messages = [r.getMessage() for r in caplog.records]
    assert any("HTTP 502; attempt 1, retrying in" in m for m in messages)
    assert any("secondary rate limit" in m and "pausing 7s" in m for m in messages)
    assert any("exhausted; waiting" in m for m in messages)


def test_network_retry_logs_the_cause(instant_sleep, caplog):
    with caplog.at_level("WARNING", logger="scanner.github"):
        gh, _ = make_client([httpx.ConnectError("boom"), (200, {"ok": True}, {})])
        assert gh.get("/repos/acme/demo") == {"ok": True}
    assert any("network error (boom); attempt 1, retrying in" in r.getMessage() for r in caplog.records)


def test_graphql_rate_limited_rotates_and_retries(instant_sleep):
    reset = str(int(time.time()) + 3600)
    gh, requests = make_client([
        (200, {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "slow down"}]},
         {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
        (200, {"data": {"ok": 1}}, {}),
    ], token=["tok-a", "tok-b"])
    assert gh.graphql("query {}", {}) == {"ok": 1}
    assert requests[1].headers["authorization"] == "Bearer tok-b"
