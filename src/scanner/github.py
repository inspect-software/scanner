"""Minimal GitHub REST API client for public repository data.

Works unauthenticated (60 requests/hour); provide a token for the
5000 requests/hour per authenticated token. Token resolution order:
explicit argument (CLI --token) > GITHUB_TOKEN / GH_TOKEN plus optional
GITHUB_TOKENS pool from environment variables or a .env file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

API_BASE = "https://api.github.com"

TOKEN_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
TOKEN_POOL_VAR = "GITHUB_TOKENS"

# A scan creates a new client, while the website worker keeps running. Remember
# exhausted tokens for that worker process so every following scan does not
# spend a request rediscovering the same rate limit. Token values are used only
# as in-memory keys and are never written to logs.
_token_cooldowns: dict[str, float] = {}
logger = logging.getLogger(__name__)

# Optional cross-process observer. A long-running host (the website scan worker)
# may set this to persist rate-limit events for an operator dashboard. It is
# called whenever a token is marked exhausted, receiving the token's
# non-reversible fingerprint (never the token value), its 0-based pool slot, the
# pool size, the unix reset time, and the HTTP status. Exceptions are swallowed
# so observability can never break a scan.
RateLimitObserver = Callable[[str, int, int, float, int], None]
rate_limit_observer: Optional[RateLimitObserver] = None


def token_fingerprint(token: str) -> str:
    """Stable, non-reversible identifier for a token, safe to store and display."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _notify_rate_limit(token: str, index: int, count: int, reset_epoch: float, status: int) -> None:
    observer = rate_limit_observer
    if observer is None:
        return
    try:
        observer(token_fingerprint(token), index, count, reset_epoch, status)
    except Exception:  # pragma: no cover - observability must never break a scan
        logger.debug("rate_limit_observer raised", exc_info=True)


def resolve_token(explicit: Optional[str] = None, env_file: str = ".env") -> Optional[str]:
    """Resolve a GitHub token: explicit arg > environment > .env file."""
    if explicit:
        return explicit
    for var in TOKEN_VARS:
        value = os.environ.get(var)
        if value:
            return value
    dotenv = dotenv_values(env_file)
    for var in TOKEN_VARS:
        value = dotenv.get(var)
        if value:
            return value
    return None


def _split_tokens(value: str | Sequence[str] | None) -> list[str]:
    """Normalize a comma-separated token value without exposing token values."""
    if value is None:
        return []
    values = value.split(",") if isinstance(value, str) else value
    result: list[str] = []
    for item in values:
        token = item.strip()
        if token and token not in result:
            result.append(token)
    return result


def resolve_tokens(
    explicit: str | Sequence[str] | None = None, env_file: str = ".env"
) -> list[str]:
    """Resolve a GitHub token pool.

    A single explicit CLI token remains the first choice. ``GITHUB_TOKENS``
    adds comma-separated secondary tokens; the legacy ``GITHUB_TOKEN`` and
    ``GH_TOKEN`` values stay supported and are always tried first.
    """
    explicit_tokens = _split_tokens(explicit)
    if explicit_tokens:
        return explicit_tokens

    dotenv = dotenv_values(env_file)
    values: list[str] = []
    for source in (os.environ, dotenv):
        for var in TOKEN_VARS:
            values.extend(_split_tokens(source.get(var)))
        values.extend(_split_tokens(source.get(TOKEN_POOL_VAR)))
    return _split_tokens(values)

# GitHub statistics endpoints return 202 while the data is being computed.
STATS_RETRIES = 3
STATS_RETRY_DELAY_SECONDS = 2.0


class GitHubError(Exception):
    """Fatal error talking to the GitHub API."""


class RepoNotFoundError(GitHubError):
    """Repository does not exist or is not publicly accessible."""


def parse_target(value: str) -> tuple[str, ...]:
    """Classify a scan target.

    Returns ``("repo", owner, name)`` for repository targets and
    ``("org", login)`` for single-segment targets (an owner URL or bare
    account name, e.g. ``https://github.com/python`` or ``python``).
    """
    candidate = value.strip().rstrip("/")
    try:
        owner, name = parse_repo_url(candidate)
        return ("repo", owner, name)
    except ValueError:
        pass

    if re.match(r"^[\w][\w-]*$", candidate):
        return ("org", candidate)
    parsed = urlparse(candidate)
    if parsed.netloc.lower() in ("github.com", "www.github.com"):
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) == 1:
            return ("org", parts[0])
    raise ValueError(
        f"Cannot interpret target {value!r}: expected a repository "
        "(owner/name or GitHub repo URL) or an organization (login or GitHub org URL)"
    )


def parse_repo_url(url: str) -> tuple[str, str]:
    """Extract (owner, name) from a GitHub repo URL or ``owner/name`` shorthand.

    Accepts https URLs, git+ssh URLs (git@github.com:owner/name.git),
    and bare ``owner/name``.
    """
    candidate = url.strip()

    ssh_match = re.match(r"^git@github\.com:(?P<path>.+)$", candidate)
    if ssh_match:
        path = ssh_match.group("path")
    elif re.match(r"^[\w.-]+/[\w.-]+$", candidate):
        path = candidate
    else:
        parsed = urlparse(candidate)
        if parsed.netloc.lower() not in ("github.com", "www.github.com"):
            raise ValueError(
                f"Not a GitHub repository URL: {url!r} "
                "(expected https://github.com/owner/name, git@github.com:owner/name, or owner/name)"
            )
        path = parsed.path

    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Cannot extract owner/name from: {url!r}")
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return owner, name


class GitHubClient:
    def __init__(self, token: str | Sequence[str] | None = None, timeout: float = 30.0):
        self.tokens = resolve_tokens(token)
        self._token_index = self._first_available_token_index()
        self.token = self.tokens[self._token_index] if self._token_index is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "inspect-scanner",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(base_url=API_BASE, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """GET a JSON endpoint; raises GitHubError on failure.

        ``timeout`` overrides the client default for this request only — used
        by time-budgeted collection steps (e.g. the dependency-graph SBOM).
        """
        response = self._request(path, params, timeout=timeout)
        if response.status_code == 404:
            raise RepoNotFoundError(f"Not found: {path}")
        if response.status_code in (403, 429) and response.headers.get(
            "x-ratelimit-remaining"
        ) == "0":
            reset = response.headers.get("x-ratelimit-reset", "?")
            raise GitHubError(
                f"GitHub API rate limit exceeded (resets at unix time {reset}). "
                "Provide a token (--token, GITHUB_TOKEN/GH_TOKEN env var, or .env file) "
                "to raise the limit to 5000 requests/hour."
            )
        if response.status_code >= 400:
            raise GitHubError(f"GitHub API error {response.status_code} for {path}")
        return response.json()

    def get_optional(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Like ``get`` but returns None on 404 instead of raising."""
        try:
            return self.get(path, params, timeout=timeout)
        except RepoNotFoundError:
            return None

    def get_stats(self, path: str) -> Any:
        """GET a /stats/* endpoint, retrying while GitHub computes it (HTTP 202).

        Returns None if the stats are still not ready after retries.
        """
        for attempt in range(STATS_RETRIES):
            response = self._request(path, None)
            if response.status_code == 202:
                time.sleep(STATS_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            if response.status_code == 204 or not response.content:
                return None
            if response.status_code >= 400:
                return None
            return response.json()
        return None

    def search_count(self, query: str) -> Optional[int]:
        """Return total_count for a GitHub search query, or None on failure.

        The search API has its own (much lower) rate limit, so failures here
        are treated as soft: the caller records a warning instead of aborting.
        """
        response = self._request("/search/issues", {"q": query, "per_page": 1})
        if response.status_code >= 400:
            return None
        data = response.json()
        return data.get("total_count")

    def _request(
        self,
        path: str,
        params: Optional[dict[str, Any]],
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        request_timeout = timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
        try:
            response = self._client.get(path, params=params, timeout=request_timeout)
            while self._rate_limited(response) and self._rotate_token(response):
                response = self._client.get(path, params=params, timeout=request_timeout)
            return response
        except httpx.HTTPError as exc:
            raise GitHubError(f"Network error requesting {path}: {exc}") from exc

    @staticmethod
    def _rate_limited(response: httpx.Response) -> bool:
        return response.status_code == 429 or (
            response.status_code == 403
            and response.headers.get("x-ratelimit-remaining") == "0"
        )

    def _first_available_token_index(self) -> int | None:
        now = time.time()
        for index, token in enumerate(self.tokens):
            if _token_cooldowns.get(token, 0) <= now:
                return index
        return 0 if self.tokens else None

    def _rotate_token(self, response: httpx.Response) -> bool:
        """Remember an exhausted token and switch to another usable token."""
        if self._token_index is None:
            return False
        reset = response.headers.get("x-ratelimit-reset")
        try:
            until = float(reset) if reset else time.time() + 60
        except ValueError:
            until = time.time() + 60
        until = max(until, time.time() + 1)
        _token_cooldowns[self.tokens[self._token_index]] = until
        _notify_rate_limit(
            self.tokens[self._token_index], self._token_index, len(self.tokens), until, response.status_code
        )

        previous = self._token_index
        now = time.time()
        for offset in range(1, len(self.tokens)):
            candidate = (previous + offset) % len(self.tokens)
            if _token_cooldowns.get(self.tokens[candidate], 0) > now:
                continue
            self._token_index = candidate
            self.token = self.tokens[candidate]
            self._client.headers["Authorization"] = f"Bearer {self.token}"
            logger.warning(
                "GitHub API rate limited; rotating token slot %d/%d to %d/%d",
                previous + 1, len(self.tokens), candidate + 1, len(self.tokens),
            )
            return True
        return False
