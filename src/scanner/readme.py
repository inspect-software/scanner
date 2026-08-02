"""What a README displays as status badges.

A badge row is the strip of small SVG images most READMEs open with: build
status, coverage, package version, license, downloads. It is read here for two
questions that nothing else in the report answers.

The first is descriptive. A project that publishes its build and coverage state
in the README is making a claim in public and keeping it current; the badge row
is the maintainer's own summary of the project's health. This is *not* scored —
every underlying fact a badge asserts (CI runs, coverage exists, a release is
published) is already measured directly from the repository, and scoring the
picture of the fact on top of the fact would double-count it. Worse, it would
be the cheapest signal in the report to fake: a badge is one line of Markdown
and nothing verifies that it points at this project. The counts are carried as
inputs so a reader can see them, and they carry no weight.

The second is operational: whether *our own* badge is already among them.
``has_inspect_badge`` is the only reliable way to tell adoption from
publication — the website records that it pushed an SVG, which says nothing
about whether the maintainer ever embedded it.

Detection is by image URL, never by alt text. Alt text is free-form and
routinely absent; the URL is what the browser actually fetches. Both the
Markdown and HTML image forms are read (READMEs mix them freely, and the
common shape — a badge wrapped in a link — is a Markdown image inside a
Markdown link), plus reStructuredText's directive for README.rst.

``BADGE_HOSTS`` is inevitably incomplete: new badge services appear and
projects self-host SVGs. A missed badge reads as no badge, which is the safe
direction to fail for a figure that is descriptive only.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

# Hosts that serve status badges and effectively nothing else. Matched on the
# registered domain (or a suffix of the host), so subdomains such as
# `camo.githubusercontent.com` proxying elsewhere are not mistaken for these.
BADGE_HOSTS = (
    "shields.io",
    "badgen.net",
    "badge.fury.io",
    "codecov.io",
    "coveralls.io",
    "app.codacy.com",
    "api.codeclimate.com",
    "sonarcloud.io",
    "snyk.io",
    "travis-ci.com",
    "travis-ci.org",
    "circleci.com",
    "ci.appveyor.com",
    "dev.azure.com",
    "readthedocs.org",
    "pkg.go.dev",
    "goreportcard.com",
    "isitmaintained.com",
    "opencollective.com",
    "bestpractices.coreinfrastructure.org",
    "www.bestpractices.dev",
    "bestpractices.dev",
    "api.securityscorecards.dev",
    "deps.dev",
    "jitpack.io",
    "poser.pugx.org",
)

# GitHub serves its own workflow badges from the repository URL space rather
# than a badge host, so they are recognized by path shape instead.
_GITHUB_BADGE_PATH = re.compile(r"/(actions/workflows/[^/]+/badge\.svg|badge\.svg|workflows/[^/]+/badge\.svg)")

# Our own badge is handed out in two shapes: a site URL, and — once the badge
# publisher has pushed it — a GitHub CDN URL under the badges repository, whose
# host is somebody else's and whose path is only `…/v1/<shard>/<owner>/<repo>`.
# The CDN form is therefore matched against the scanned repository's own name
# rather than by host. Matching a bare `/v1/` would collect every unrelated
# versioned asset path on the internet.
INSPECT_BADGE_HOSTS = ("inspect.software", "osaudit.org")
INSPECT_BADGE_HOST_LABEL = "inspect.software"

# Lines of README read as the header block. Badges that sit above the first
# prose or the first section heading are the "badge row" proper — the strip a
# maintainer curates and would extend. Badges buried further down (a table of
# sub-package versions, say) are a different thing, and a badge PR against them
# is not the same ask.
HEADER_LINES = 25

# Markdown `![alt](url "title")`, tolerating the angle-bracket URL form.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
# HTML `<img src="url">`, either quoting style, attributes in any order.
_HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
# reStructuredText `.. image:: url`, for README.rst.
_RST_IMAGE = re.compile(r"^\s*\.\.\s+image::\s*(\S+)", re.MULTILINE)

# A section heading ends the header block: `## Anything` in Markdown, or an
# underlined setext heading. Only level 2 and deeper — the level-1 heading is
# the project title, which sits above or among the badges.
_SECTION_HEADING = re.compile(r"^\s{0,3}#{2,6}\s", re.MULTILINE)


class ReadmeBadgeScan(NamedTuple):
    total: int
    header: int
    hosts: list[str]
    has_inspect_badge: bool


def _image_urls(text: str) -> list[tuple[int, str]]:
    """Every image URL in the document as (offset, url), in document order."""
    matches = [
        (m.start(), m.group(1))
        for pattern in (_MD_IMAGE, _HTML_IMAGE, _RST_IMAGE)
        for m in pattern.finditer(text)
    ]
    matches.sort()
    return matches


def _host_of(url: str) -> str:
    # Protocol-relative URLs (`//img.shields.io/...`) are common in older
    # READMEs and parse with an empty scheme, which urlsplit still handles.
    parts = urlsplit(url if "//" in url else f"//{url}")
    return (parts.hostname or "").lower()


def _is_inspect_badge(url: str, full_name: Optional[str]) -> bool:
    """Whether a URL is this project's own badge for ``full_name``."""
    lowered = url.lower()
    host = _host_of(url)
    if any(host == known or host.endswith("." + known) for known in INSPECT_BADGE_HOSTS):
        return True
    if not full_name:
        return False
    # `badges.badge_repo_relative_path`: v1/<first char of owner>/<owner>/<repo>.svg
    owner = full_name.lower().split("/", 1)[0]
    shard = owner[:1] if owner[:1].isascii() and owner[:1].isalnum() else "_"
    return f"/v1/{shard}/{full_name.lower()}.svg" in lowered


def badge_host(url: str, full_name: Optional[str] = None) -> Optional[str]:
    """The badge service a URL points at, or None when it is not a badge.

    Returns the matched host rather than a boolean so the report can name which
    services a project uses — the distribution across a corpus is the part that
    is actually interesting, and a bare count hides it.
    """
    lowered = url.lower()
    if _is_inspect_badge(url, full_name):
        return INSPECT_BADGE_HOST_LABEL
    host = _host_of(url)
    if not host:
        return None
    for known in BADGE_HOSTS:
        if host == known or host.endswith("." + known):
            return known
    if host in ("github.com", "raw.githubusercontent.com") and _GITHUB_BADGE_PATH.search(lowered):
        return "github.com"
    return None


def _header_cutoff(text: str) -> int:
    """Character offset where the README's header block ends."""
    heading = _SECTION_HEADING.search(text)
    lines = text.splitlines(keepends=True)
    by_line = len("".join(lines[:HEADER_LINES]))
    return min(heading.start(), by_line) if heading else by_line


def scan_readme(text: Optional[str], full_name: Optional[str] = None) -> ReadmeBadgeScan:
    """Count the badges a README displays.

    ``full_name`` is the scanned repository's ``owner/repo``; without it the
    published GitHub CDN form of our own badge cannot be told apart from any
    other asset on that host, and ``has_inspect_badge`` will only catch the
    site-hosted form.

    Duplicate URLs are counted once: a README that repeats its coverage badge
    in a per-package table displays one coverage badge as far as this is
    concerned, and counting the repeats would make the figure a function of
    document layout rather than of what the project publishes.
    """
    if not text:
        return ReadmeBadgeScan(0, 0, [], False)

    cutoff = _header_cutoff(text)
    seen: dict[str, tuple[str, bool]] = {}
    for offset, url in _image_urls(text):
        if url in seen:
            continue
        host = badge_host(url, full_name)
        if host is None:
            continue
        seen[url] = (host, offset < cutoff)

    hosts = sorted({host for host, _ in seen.values()})
    return ReadmeBadgeScan(
        total=len(seen),
        header=sum(1 for _, in_header in seen.values() if in_header),
        hosts=hosts,
        has_inspect_badge=INSPECT_BADGE_HOST_LABEL in hosts,
    )
