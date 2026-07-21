"""Canonical GitHub repository-URL extraction.

This lives in the scanner because both extraction paths need it: the website
catalog (``app/catalog/*``) decides eligibility from it, and the scanner's own
registry fetchers (``scanner.ecosystems``) record it on every report. They used
to normalise separately, so the same package could be canonical in one path and
raw in the other.

Two ideas are kept apart on purpose:

* ``repository``-style fields are *authoritative* — the publisher is naming the
  source of this exact package, so any GitHub URL there is taken as-is.
* ``bugs``/``homepage`` are *corroborating* — npm auto-derives them from
  ``repository`` at publish time, which is why they usually agree, but they are
  also where people put a docs site, a GitHub Pages repo, or an unrelated
  tracker. A GitHub URL found there is only accepted when the repository
  plausibly belongs to the package (see ``repository_suits_package``).
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

# git@github.com:owner/repo.git and ssh://git@github.com/owner/repo.git
_SCP_STYLE = re.compile(r"^(?:ssh://)?git@github\.com[:/]", re.IGNORECASE)
# A bare host, which npm packages do publish: github.com:owner/repo.git
_BARE_HOST = re.compile(r"^github\.com[:/]", re.IGNORECASE)
# owner.github.io is a Pages site; as a *homepage* it rarely means "this is the
# package's source", it means "this is where the docs are rendered from".
_PAGES_REPO = re.compile(r"^[^/]+\.github\.(io|com)$", re.IGNORECASE)

#: First path segments on github.com that are site routes, never repositories.
_RESERVED_OWNERS = frozenset({
    "about", "apps", "collections", "contact", "enterprise", "events",
    "features", "join", "login", "marketplace", "orgs", "pricing", "readme",
    "security", "settings", "signup", "site", "sponsors", "topics", "trending",
})

#: Below this length a substring match between a package name and a repository
#: name is coincidence rather than evidence ("js" is inside almost everything).
_MIN_AFFINITY_LENGTH = 3

#: What GitHub actually permits in an account name: alphanumerics and single
#: internal hyphens, 39 characters at most.
_VALID_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
#: Repository names additionally allow dot and underscore, and cannot be the
#: relative-path names.
_VALID_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def is_valid_repo_path(owner: str, repo: str) -> bool:
    """Could this owner/repo pair name an actual GitHub repository?

    Registries publish build-time placeholders verbatim — a Maven pom whose
    ``<scm>`` still reads ``${github.org}/${github.name}`` parses as a
    structurally valid URL, and one such row reached the production catalogue.
    Charset validation rejects those on the ``$`` and ``{`` without needing a
    placeholder-syntax blocklist.
    """
    if not _VALID_OWNER.match(owner) or not _VALID_REPO.match(repo):
        return False
    if repo in {".", ".."}:
        return False
    return owner.lower() not in _RESERVED_OWNERS


def _slug(value: str) -> str:
    """Strip everything that varies freely between a package and its repo name."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def github_repository_url(value: Optional[str]) -> Optional[str]:
    """Return a canonical public GitHub URL without making a GitHub request.

    Accepts every transport form registries publish — ``git+https://``,
    ``git://``, ``git+ssh://git@github.com/``, ``git@github.com:`` and a bare
    ``github.com:`` — and discards the trailing ``.git`` and any path below the
    repository (``/issues``, ``/tree/main/packages/x``).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().removeprefix("git+")
    candidate = _SCP_STYLE.sub("https://github.com/", candidate)
    candidate = _BARE_HOST.sub("https://github.com/", candidate)
    for scheme in ("git://", "ssh://"):
        if candidate.lower().startswith(scheme):
            candidate = "https://" + candidate[len(scheme):]
            break
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not is_valid_repo_path(owner, repo):
        return None
    return f"https://github.com/{owner}/{repo}"


def repository_suits_package(package_name: str, repository_url: str) -> bool:
    """Is ``repository_url`` plausibly the source of ``package_name``?

    Used to gate the corroborating fields only. The test is deliberately loose
    — it exists to reject a link to somebody else's repository, not to demand
    that a project name its repo after its package.
    """
    if not package_name or not repository_url:
        return False
    owner, _, repo = repository_url.rpartition("/")
    owner = owner.rpartition("/")[2]
    if not owner or not repo:
        return False
    # `.github` holds org profile/workflow templates; a Pages repo holds a site.
    if repo.lower() == ".github" or _PAGES_REPO.match(repo):
        return False
    scope = package_name.split("/")[0].lstrip("@") if package_name.startswith("@") else ""
    base = _slug(package_name.split("/")[-1])
    owner_slug, repo_slug = _slug(owner), _slug(repo)
    if scope and _slug(scope) in {owner_slug, repo_slug}:
        return True
    if len(base) < _MIN_AFFINITY_LENGTH:
        return False
    for other in (repo_slug, owner_slug):
        if len(other) < _MIN_AFFINITY_LENGTH:
            continue
        if base == other or base in other or other in base:
            return True
    return False


def url_of(value: Any) -> Optional[str]:
    """npm writes repository/bugs as either a string or a {type, url} object."""
    if isinstance(value, dict):
        value = value.get("url")
    return value if isinstance(value, str) else None


def npm_source_url(version_meta: dict[str, Any], packument: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Extract a package's GitHub repository from an npm packument.

    ``repository`` first (authoritative), then ``bugs``, then ``homepage`` —
    the latter two only when they suit the package name. Each field is read
    from the latest version document before the packument root, because the
    root can lag behind by years.
    """
    packument = packument or {}
    name = str(version_meta.get("name") or packument.get("name") or "")

    def field(key: str) -> Optional[str]:
        return url_of(version_meta.get(key) if version_meta.get(key) is not None else packument.get(key))

    authoritative = github_repository_url(field("repository"))
    if authoritative:
        return authoritative
    for key in ("bugs", "homepage"):
        corroborating = github_repository_url(field(key))
        if corroborating and repository_suits_package(name, corroborating):
            return corroborating
    return None


def display_repository_url(value: Optional[str]) -> Optional[str]:
    """Canonicalise for display, keeping non-GitHub hosts rather than dropping
    them — reports show a GitLab or Codeberg URL, only the catalog requires
    GitHub."""
    canonical = github_repository_url(value)
    if canonical:
        return canonical
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().removeprefix("git+").removesuffix(".git") or None
