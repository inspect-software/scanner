"""Minimal GitHub REST API client for public repository data.

Works unauthenticated (60 requests/hour per IP); provide a token for
5000 requests/hour. That budget belongs to the authenticated **user**, not to
the token: two tokens minted by the same account share one 5000/hour bucket and
pooling them buys nothing. Only tokens from different accounts add headroom.

Token resolution order: explicit argument (CLI --token) > GITHUB_TOKEN /
GH_TOKEN plus optional GITHUB_TOKENS pool from environment variables or a .env
file.

Two limits, worth keeping apart. The *primary* one above is a budget, it reports
x-ratelimit-remaining, and rotating to another account's token answers it. A
*secondary* ("abuse") limit is GitHub refusing a request **rate** regardless of
remaining budget; it arrives as 403/429 with Retry-After, or as a bare 503 on
every endpoint except /rate_limit — the one endpoint exempt from the limiter,
and so the tell that a throttle rather than a spent token is at work. Rotation
does not help there. Only slowing down does: see min_request_interval_seconds.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

from .repourl import is_valid_repo_path

API_BASE = "https://api.github.com"

TOKEN_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
TOKEN_POOL_VAR = "GITHUB_TOKENS"

# A scan creates a new client, while the website worker keeps running. Remember
# exhausted tokens for that worker process so every following scan does not
# spend a request rediscovering the same rate limit. Token values are used only
# as in-memory keys and are never written to logs.
_token_cooldowns: dict[str, float] = {}

# Headroom last reported by GitHub per token (x-ratelimit-remaining, free on
# every response). Each scan builds a new client, so without this every scan
# would start at slot 0 and spend it down to nothing while the later slots sat
# untouched — which is exactly what happened in production on 2026-07-17: one
# account drained to 5000/5000 and collected a scraping warning while a sibling
# had used a single request. Choosing the roomiest token instead spreads a queue
# of scans across the accounts that can afford it.
_token_remaining: dict[str, int] = {}

# What GitHub grants an authenticated user per hour. Only used as the assumed
# headroom of a token we have not called yet, so an untried token is preferred
# over one we have already spent.
ASSUMED_HOURLY_LIMIT = 5000

logger = logging.getLogger(__name__)

# Pace. Secondary ("abuse") limits key off request *rate*, not remaining budget:
# GitHub's own guidance is to avoid concurrent requests and make them serially.
# A burst can therefore be refused while every token still has thousands of
# requests left — as it was in production, where a fresh unused token was
# throttled identically to a spent one. Zero by default so a one-off CLI scan is
# never slowed; the bulk hosts (the scan workers) set
# GITHUB_MIN_REQUEST_INTERVAL_SECONDS to spread their traffic out.
def _default_min_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("GITHUB_MIN_REQUEST_INTERVAL_SECONDS") or 0.0))
    except ValueError:
        return 0.0


min_request_interval_seconds: float = _default_min_interval()
_last_request_at: float = 0.0
# The website's request path calls this client from a thread pool, so the pacing
# clock is shared across threads in one process.
_pace_lock = threading.Lock()

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

# Transient-failure policy, applied centrally in GitHubClient._send so no
# call site needs its own try/except. A scan makes dozens of requests; one
# flaky 502 or connection reset must not fail the whole job.
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 1.0
# Retries are bounded by wall clock, not by a count. A count meant a 5xx got
# ~7 seconds of patience: fine for one flaky response, useless for a real
# outage — on 2026-07-16 GitHub returned 503 to ~35% of REST requests for over
# an hour and every job burned its four attempts and died. Five minutes rides
# out a bad patch without a worker sitting on a job forever; anything longer is
# the job queue's problem, not one request's.
RETRY_BUDGET_SECONDS = 300.0
# Cap the exponential so a long budget cannot turn into one enormous sleep.
BACKOFF_MAX_SECONDS = 30.0
# Secondary rate limits (403/429 with Retry-After but budget remaining) ask
# for short pauses; never honor an ask longer than this.
SECONDARY_LIMIT_MAX_WAIT_SECONDS = 120.0
# When every token in the pool is exhausted but the earliest reset is close,
# wait it out instead of failing the job.
RATE_LIMIT_MAX_WAIT_SECONDS = 300.0


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
    # Last gate before a catalogue row is written: registries publish
    # unexpanded build placeholders (``${github.org}/${github.name}``) that are
    # structurally valid but name no repository.
    if not is_valid_repo_path(owner, name):
        raise ValueError(f"Not a valid GitHub owner/name: {url!r}")
    return owner, name


class GitHubClient:
    def __init__(
        self,
        token: str | Sequence[str] | None = None,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.tokens = resolve_tokens(token)
        self._token_index = self._first_available_token_index()
        self.token = self.tokens[self._token_index] if self._token_index is not None else None
        # Requests actually sent to GitHub (including retries), for operator
        # visibility and the scan-cost comparisons in scanner/scripts.
        self.requests_made = 0
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "inspect-scanner",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Renamed/transferred repos 301-redirect to their new location; without
        # following it, the response body is a small {"message": "Moved
        # Permanently", "url": ...} stub that gets misread as real resource
        # data (e.g. a "languages" map whose values are strings, not byte
        # counts — see inspect-software/workspace#8).
        self._client = httpx.Client(
            base_url=API_BASE,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

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

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a GraphQL query and return its ``data``; raises GitHubError.

        GraphQL draws from its own 5000-points/hour budget, separate from the
        REST limit. It requires authentication; callers must be prepared to
        fall back to REST when ``self.token`` is None. NOT_FOUND errors map to
        RepoNotFoundError, mirroring the REST 404 behavior. A rate-limited
        response rotates tokens exactly like REST and retries.
        """
        if not self.token:
            raise GitHubError("GitHub GraphQL API requires an authentication token")
        while True:
            response = self._send("POST", "/graphql", json={"query": query, "variables": variables})
            if self._rate_limited(response) or response.status_code >= 400:
                raise GitHubError(f"GitHub GraphQL request failed with HTTP {response.status_code}")
            payload = response.json()
            errors = payload.get("errors") or []
            if not errors:
                return payload.get("data") or {}
            kinds = {e.get("type") for e in errors}
            if "NOT_FOUND" in kinds:
                raise RepoNotFoundError(errors[0].get("message", "Not found"))
            # The GraphQL budget signals exhaustion inside a 200 body; reuse
            # the REST rotation (the response carries x-ratelimit-* headers).
            if "RATE_LIMITED" in kinds and self._rotate_token(response):
                continue
            raise GitHubError("GitHub GraphQL error: " + "; ".join(
                e.get("message", e.get("type", "unknown")) for e in errors
            ))

    def _request(
        self,
        path: str,
        params: Optional[dict[str, Any]],
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        return self._send("GET", path, params=params, timeout=timeout)

    def _send(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        """Send one API request with the full resilience policy applied.

        Centralizes every transient-failure answer so no caller carries its
        own try/except: network errors and 5xx responses back off and retry
        for up to RETRY_BUDGET_SECONDS; a 5xx while authenticated also tries
        the request anonymously, since GitHub can fail authenticated requests
        while serving anonymous ones; secondary rate limits honor Retry-After
        (bounded); a primary rate-limit exhaustion rotates to the next usable
        pool token, and when the whole pool is cooling but the earliest reset
        is near, waits it out. Anything still failing when the budget runs out
        is returned to the caller, which turns it into a GitHubError.
        """
        request_timeout = timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
        deadline = time.monotonic() + RETRY_BUDGET_SECONDS
        attempt = 0
        tried_anonymous = False
        while True:
            try:
                self._pace()
                self.requests_made += 1
                response = self._client.request(
                    method, path, params=params, json=json, timeout=request_timeout
                )
            except httpx.HTTPError as exc:
                attempt += 1
                delay = self._backoff(attempt)
                if self._out_of_budget(deadline, delay):
                    logger.error(
                        "GitHub %s %s: network error (%s); giving up after %d attempt(s) over %.0fs",
                        method, path, exc, attempt, RETRY_BUDGET_SECONDS,
                    )
                    raise GitHubError(f"Network error requesting {path}: {exc}") from exc
                logger.warning(
                    "GitHub %s %s: network error (%s); attempt %d, retrying in %.0fs",
                    method, path, exc, attempt, delay,
                )
                self._sleep(delay)
                continue

            self._record_remaining(response)

            if response.status_code in TRANSIENT_STATUSES:
                # GitHub is failing, not refusing. It can fail authenticated
                # requests while still serving anonymous ones — that is exactly
                # what its 2026-07-16 incident did — so an authenticated 5xx is
                # worth one anonymous attempt before waiting. Public repository
                # data is all this client reads, so the anonymous answer is the
                # same answer; only the 60/hour IP budget differs, which is why
                # it is a fallback and not the default.
                if self.token and not tried_anonymous and method == "GET":
                    tried_anonymous = True
                    anonymous = self._send_anonymous(method, path, params, request_timeout)
                    if anonymous is not None:
                        logger.warning(
                            "GitHub %s %s: HTTP %d authenticated, served anonymously instead",
                            method, path, response.status_code,
                        )
                        return anonymous

                attempt += 1
                delay = self._backoff(attempt)
                if self._out_of_budget(deadline, delay):
                    logger.error(
                        "GitHub %s %s: HTTP %d; giving up after %d attempt(s) over %.0fs",
                        method, path, response.status_code, attempt, RETRY_BUDGET_SECONDS,
                    )
                    return response
                logger.warning(
                    "GitHub %s %s: HTTP %d; attempt %d, retrying in %.0fs",
                    method, path, response.status_code, attempt, delay,
                )
                self._sleep(delay)
                continue

            if self._rate_limited(response):
                # Primary budget exhausted: rotation is free (does not count
                # as an attempt) and bounded by the pool size.
                if self._rotate_token(response):
                    continue
                wait = self._seconds_until_earliest_reset()
                if wait is not None and wait <= RATE_LIMIT_MAX_WAIT_SECONDS:
                    logger.warning(
                        "GitHub rate limit: all %d token(s) exhausted; waiting %.0fs for the earliest reset",
                        len(self.tokens), wait,
                    )
                    self._sleep(wait)
                    self._activate_token(self._first_available_token_index())
                    continue
                return response  # caller reports the exhaustion

            if response.status_code in (403, 429) and "retry-after" in response.headers:
                # Secondary (abuse) rate limit: budget remains, GitHub just
                # asks for a pause.
                attempt += 1
                try:
                    asked = float(response.headers["retry-after"])
                except ValueError:
                    asked = BACKOFF_BASE_SECONDS
                pause = min(asked, SECONDARY_LIMIT_MAX_WAIT_SECONDS)
                if self._out_of_budget(deadline, pause):
                    logger.error(
                        "GitHub secondary rate limit on %s %s; giving up after %.0fs",
                        method, path, RETRY_BUDGET_SECONDS,
                    )
                    return response
                logger.warning(
                    "GitHub secondary rate limit on %s %s; pausing %.0fs (Retry-After)",
                    method, path, pause,
                )
                self._sleep(pause)
                continue

            return response

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)

    @staticmethod
    def _out_of_budget(deadline: float, planned_wait: float) -> bool:
        """Whether waiting `planned_wait` would overrun the retry budget.

        Checked before sleeping rather than after, so the budget is a promise
        about when the caller hears back, not a bound that is discovered by
        exceeding it.
        """
        return time.monotonic() + planned_wait >= deadline

    def _send_anonymous(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]],
        request_timeout: Any,
    ) -> Optional[httpx.Response]:
        """One unauthenticated attempt; None if it is no better than the last.

        Its own client, so the pool's Authorization header cannot leak into it
        and its 60/hour IP budget stays separate from the token budgets.
        """
        headers = {k: v for k, v in self._client.headers.items() if k.lower() != "authorization"}
        try:
            self._pace()
            self.requests_made += 1
            with httpx.Client(base_url=API_BASE, headers=headers, follow_redirects=True) as client:
                response = client.request(method, path, params=params, timeout=request_timeout)
        except httpx.HTTPError as exc:
            logger.debug("anonymous retry of %s %s failed: %s", method, path, exc)
            return None
        # 403 here is the 60/hour IP budget, which says nothing useful; anything
        # 5xx means GitHub is failing anonymously too. Either way, fall back to
        # waiting on the authenticated path.
        if response.status_code >= 400:
            return None
        return response

    @staticmethod
    def _sleep(seconds: float) -> None:
        """Single overridable pause point (patched to zero in tests)."""
        time.sleep(max(seconds, 0.0))

    def _pace(self) -> None:
        """Hold requests to at most one per min_request_interval_seconds.

        Process-wide rather than per-client: the point is the rate GitHub sees,
        and a host builds a client per scan. Zero (the default) is a no-op, so
        the CLI is never slowed.
        """
        interval = min_request_interval_seconds
        if interval <= 0:
            return
        global _last_request_at
        with _pace_lock:
            wait = _last_request_at + interval - time.monotonic()
            # Claim the slot before releasing the lock so concurrent threads
            # queue behind each other instead of all reading the same clock and
            # firing together — which would be the burst this exists to prevent.
            _last_request_at = max(time.monotonic(), _last_request_at + interval)
        if wait > 0:
            self._sleep(wait)

    def _record_remaining(self, response: httpx.Response) -> None:
        """Note the headroom GitHub reports, to steer the next token choice."""
        if not self.token:
            return
        raw = response.headers.get("x-ratelimit-remaining")
        if raw is None:
            return
        try:
            _token_remaining[self.token] = int(raw)
        except ValueError:
            pass

    def _seconds_until_earliest_reset(self) -> Optional[float]:
        """Seconds until the soonest token cooldown lifts, or None without tokens."""
        if not self.tokens:
            return None
        earliest = min(_token_cooldowns.get(token, 0.0) for token in self.tokens)
        return max(earliest - time.time(), 0.0) + 1.0

    def _activate_token(self, index: Optional[int]) -> None:
        if index is None:
            return
        self._token_index = index
        self.token = self.tokens[index]
        self._client.headers["Authorization"] = f"Bearer {self.token}"

    @staticmethod
    def _rate_limited(response: httpx.Response) -> bool:
        return response.status_code == 429 or (
            response.status_code == 403
            and response.headers.get("x-ratelimit-remaining") == "0"
        )

    def _first_available_token_index(self) -> int | None:
        """Pick the usable token with the most headroom left.

        Not the first usable one: every scan builds a fresh client, so "first"
        means each scan starts on slot 0 and drains that one account while the
        rest idle. Preferring the roomiest token spreads a queue of scans over
        the whole pool instead. A token we have never called is assumed full, so
        it gets tried before one we have already spent.
        """
        now = time.time()
        candidates = [
            (index, token) for index, token in enumerate(self.tokens)
            if _token_cooldowns.get(token, 0) <= now
        ]
        if not candidates:
            return 0 if self.tokens else None
        # Ties (a fresh pool, where nothing is known yet) fall back to pool
        # order, which keeps single-token and first-run behaviour unchanged.
        return max(
            candidates,
            key=lambda pair: (_token_remaining.get(pair[1], ASSUMED_HOURLY_LIMIT), -pair[0]),
        )[0]

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
            self._activate_token(candidate)
            logger.warning(
                "GitHub API rate limited; rotating token slot %d/%d to %d/%d",
                previous + 1, len(self.tokens), candidate + 1, len(self.tokens),
            )
            return True
        return False
