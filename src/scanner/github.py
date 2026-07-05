"""Minimal GitHub REST API client for public repository data.

Works unauthenticated (60 requests/hour); set GITHUB_TOKEN for the
5000 requests/hour authenticated limit.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

API_BASE = "https://api.github.com"

# GitHub statistics endpoints return 202 while the data is being computed.
STATS_RETRIES = 3
STATS_RETRY_DELAY_SECONDS = 2.0


class GitHubError(Exception):
    """Fatal error talking to the GitHub API."""


class RepoNotFoundError(GitHubError):
    """Repository does not exist or is not publicly accessible."""


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
    def __init__(self, token: Optional[str] = None, timeout: float = 30.0):
        self.token = token or os.environ.get("GITHUB_TOKEN")
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

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        """GET a JSON endpoint; raises GitHubError on failure."""
        response = self._request(path, params)
        if response.status_code == 404:
            raise RepoNotFoundError(f"Not found: {path}")
        if response.status_code in (403, 429) and response.headers.get(
            "x-ratelimit-remaining"
        ) == "0":
            reset = response.headers.get("x-ratelimit-reset", "?")
            raise GitHubError(
                f"GitHub API rate limit exceeded (resets at unix time {reset}). "
                "Set GITHUB_TOKEN to raise the limit to 5000 requests/hour."
            )
        if response.status_code >= 400:
            raise GitHubError(f"GitHub API error {response.status_code} for {path}")
        return response.json()

    def get_optional(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Like ``get`` but returns None on 404 instead of raising."""
        try:
            return self.get(path, params)
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

    def _request(self, path: str, params: Optional[dict[str, Any]]) -> httpx.Response:
        try:
            return self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Network error requesting {path}: {exc}") from exc
