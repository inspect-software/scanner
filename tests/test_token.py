import pytest

from scanner.github import GitHubClient, resolve_token


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isolate each test: no real tokens in env, cwd without a .env file."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
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
