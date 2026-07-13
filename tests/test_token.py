import httpx
import pytest

from scanner import github
from scanner.github import GitHubClient, resolve_token, resolve_tokens, token_fingerprint


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isolate each test: no real tokens in env, cwd without a .env file."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    github._token_cooldowns.clear()
    github.rate_limit_observer = None
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_no_token_anywhere():
    assert resolve_token() is None


def test_explicit_argument_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    (tmp_path / ".env").write_text("GITHUB_TOKEN=from-dotenv\n", encoding="utf-8")
    assert resolve_token("from-cli") == "from-cli"


def test_env_var_wins_over_dotenv(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    (tmp_path / ".env").write_text("GITHUB_TOKEN=from-dotenv\n", encoding="utf-8")
    assert resolve_token() == "from-env"


def test_gh_token_env_fallback(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    assert resolve_token() == "gh-token"


def test_github_token_beats_gh_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    assert resolve_token() == "github-token"


def test_token_pool_keeps_primary_then_secondary_tokens(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "primary")
    monkeypatch.setenv("GITHUB_TOKENS", "secondary, tertiary, secondary")
    assert resolve_tokens() == ["primary", "secondary", "tertiary"]


def test_dotenv_file_used_when_env_empty(tmp_path):
    (tmp_path / ".env").write_text(
        "# comment\nGITHUB_TOKEN=from-dotenv\nOTHER=x\n", encoding="utf-8"
    )
    assert resolve_token() == "from-dotenv"


def test_dotenv_gh_token_fallback(tmp_path):
    (tmp_path / ".env").write_text("GH_TOKEN=dotenv-gh\n", encoding="utf-8")
    assert resolve_token() == "dotenv-gh"


def test_blank_values_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "")
    (tmp_path / ".env").write_text("GITHUB_TOKEN=\nGH_TOKEN=real\n", encoding="utf-8")
    assert resolve_token() == "real"


def test_client_sends_auth_header(tmp_path):
    (tmp_path / ".env").write_text("GITHUB_TOKEN=dotenv-token\n", encoding="utf-8")
    with GitHubClient() as gh:
        assert gh.token == "dotenv-token"
        assert gh._client.headers["Authorization"] == "Bearer dotenv-token"


def test_client_no_token_no_header():
    with GitHubClient() as gh:
        assert gh.token is None
        assert "Authorization" not in gh._client.headers


def test_client_rotates_token_on_rate_limit(caplog):
    calls: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Authorization", ""))
        if len(calls) == 1:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
        return httpx.Response(200, json={"ok": True})

    with GitHubClient(["primary", "secondary"]) as gh:
        gh._client = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": "Bearer primary"},
            transport=httpx.MockTransport(responder),
        )
        assert gh.get("/rate-limit-test") == {"ok": True}
        assert gh.token == "secondary"

    assert calls == ["Bearer primary", "Bearer secondary"]
    assert "rotating token slot 1/2 to 2/2" in caplog.text


def test_client_skips_token_exhausted_by_a_previous_scan(monkeypatch):
    monkeypatch.setattr(github.time, "time", lambda: 1_000.0)
    github._token_cooldowns["primary"] = 2_000.0

    with GitHubClient(["primary", "secondary"]) as gh:
        assert gh.token == "secondary"
        assert gh._client.headers["Authorization"] == "Bearer secondary"


def test_token_fingerprint_is_stable_and_hides_the_token():
    fp = token_fingerprint("secret-token")
    assert fp == token_fingerprint("secret-token")
    assert "secret-token" not in fp
    assert len(fp) == 16


def test_rate_limit_observer_is_notified_on_rotation():
    events: list[tuple] = []
    github.rate_limit_observer = lambda *args: events.append(args)

    def responder(request: httpx.Request) -> httpx.Response:
        if not events:
            return httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}
            )
        return httpx.Response(200, json={"ok": True})

    with GitHubClient(["primary", "secondary"]) as gh:
        gh._client = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": "Bearer primary"},
            transport=httpx.MockTransport(responder),
        )
        assert gh.get("/rate-limit-test") == {"ok": True}

    assert len(events) == 1
    fingerprint, index, count, reset_epoch, status = events[0]
    assert fingerprint == token_fingerprint("primary")
    assert (index, count, reset_epoch, status) == (0, 2, 9999999999.0, 403)
