"""Find llms.txt on the project's website when the repository has none.

The tree scan in ``collect._ai_readiness`` catches an ``llms.txt`` committed to
the repository, but that is not where the file usually lives: the llms.txt
convention places it at the *website* root, and documentation toolchains
(Sphinx extensions, Mintlify, Docusaurus plugins) generate it at build time.
Locust builds one since locustio/locust#3399 and serves it at
``docs.locust.io/llms.txt`` with nothing in the tree — which is exactly the
project this probe was written for.

So when the tree has no ``llms.txt``, a handful of well-known locations are
probed over HTTP: the declared homepage's root, and the root of every
documentation-looking site the README links to. The probe is existence-plus-
shape, not a content audit — a 200 that is served as text, not HTML, and
carries a Markdown heading (the spec's required H1) counts.

The URLs probed derive from the repository under audit, so every fetch goes
through the same public-host guard as the icon cascade (``icon.is_public_url``)
on a client that carries no GitHub credential.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

import httpx

from .icon import host_of, is_generic_host, is_public_url, public_stream

# The entrypoint first: a site publishing only the expanded variant exists but
# is the rare case, and probing both per base keeps the miss cheap.
LLMS_TXT_FILENAMES = ("llms.txt", "llms-full.txt")

# Bases are cheap to list and expensive to probe; a miss costs two requests
# against a host that may be slow. Stop early.
MAX_PROBE_BASES = 3

# Existence is decided from the head of the file alone; llms-full.txt can run
# to megabytes and none of it beyond the first heading matters here.
MAX_HEAD_BYTES = 64 * 1024

# Documentation lives either on a docs-named host, on a docs-hosting platform,
# or under a /docs path of the project site. Platforms that give each project
# a *path* rather than a subdomain (github.io project pages) keep that first
# path segment in the base; everywhere else the spec puts llms.txt at the root.
_DOCS_HOST_PREFIXES = ("docs.", "doc.")
_DOCS_HOST_SUFFIXES = (".readthedocs.io", ".gitbook.io", ".mintlify.app")
_PATH_PLATFORM_SUFFIXES = (".github.io", ".gitlab.io")
_DOCS_PATH_SEGMENTS = ("docs", "documentation")

_URL = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.I)


def _base_for(url: str) -> Optional[str]:
    """The probe base a URL implies, or None when it implies none.

    A documentation-looking URL nominates its site root; a project page on a
    path-per-project platform nominates root plus that first path segment.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.netloc.lower().split(":")[0]
    if is_generic_host(host):
        return None
    # Probed over https regardless of how the README spelled the link — old
    # READMEs still write http://docs.…, and without the upgrade the same site
    # would occupy two of the few probe slots.
    root = f"https://{parts.netloc}"
    segments = [s for s in parts.path.split("/") if s]
    if host.endswith(_PATH_PLATFORM_SUFFIXES):
        return f"{root}/{segments[0]}" if segments else root
    if host.startswith(_DOCS_HOST_PREFIXES) or host.endswith(_DOCS_HOST_SUFFIXES):
        return root
    if segments and segments[0].lower() in _DOCS_PATH_SEGMENTS:
        return root
    return None


def candidate_bases(homepage: Optional[str], readme_text: Optional[str]) -> list[str]:
    """Site roots worth probing, best evidence first, deduplicated.

    The declared homepage leads — it is repository metadata, not prose — and
    is probed even when it does not look like a documentation site, because
    the convention places llms.txt at the project site's root. README links
    only qualify when they look like documentation; probing every link in a
    README would turn one scan into a crawl.
    """
    bases: list[str] = []
    seen: set[str] = set()

    def add(base: Optional[str]) -> None:
        if base and base not in seen:
            seen.add(base)
            bases.append(base)

    if homepage and homepage.strip():
        url = homepage.strip()
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.hostname and not is_generic_host(
            host_of(url)
        ):
            add(_base_for(url) or f"https://{parts.netloc}")

    for match in _URL.finditer(readme_text or ""):
        add(_base_for(match.group(0).rstrip(".,;:!?")))
        if len(bases) >= MAX_PROBE_BASES:
            break

    return bases[:MAX_PROBE_BASES]


def _looks_like_llms_txt(head: bytes, content_type: str) -> bool:
    """Shape check: Markdown text, not an HTML page a soft-404 served as 200."""
    if content_type.startswith("text/html"):
        return False
    text = head.decode("utf-8", "replace").lstrip("﻿ \t\r\n")
    if not text or text[:1] == "<":
        return False
    # The spec requires the file to open with an H1; any Markdown heading in
    # the head separates real content from a plain-text error page.
    return any(line.lstrip().startswith("#") for line in text.splitlines()[:20])


def _fetch_head(client: httpx.Client, url: str) -> Optional[tuple[bytes, str, str]]:
    """(head bytes, content type, landing URL) of a 200, else None.

    Truncates at ``MAX_HEAD_BYTES`` instead of rejecting oversize — a large
    llms-full.txt is still an llms-full.txt; only its head is judged.
    """
    try:
        # Every hop is vetted before it is requested — see icon.public_stream.
        # Following the chain inside httpx and inspecting only where it landed
        # discards the body but still makes the request.
        with public_stream(
            client,
            url,
            accept="text/plain,text/markdown,*/*;q=0.5",
            timeout=httpx.Timeout(15.0, connect=8.0),
        ) as response:
            if response is None or response.status_code != 200:
                return None
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) >= MAX_HEAD_BYTES:
                    break
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            return bytes(body[:MAX_HEAD_BYTES]), content_type.lower(), str(response.url)
    except httpx.HTTPError:
        return None


def probe_llms_txt(
    client: httpx.Client, homepage: Optional[str], readme_text: Optional[str]
) -> Optional[str]:
    """The URL the project's website serves llms.txt at, or None.

    Never raises: an unreachable site or a lying 200 is a miss for that
    candidate, and the probe moves on.
    """
    for base in candidate_bases(homepage, readme_text):
        for filename in LLMS_TXT_FILENAMES:
            url = f"{base.rstrip('/')}/{filename}"
            if not is_public_url(url):
                continue
            fetched = _fetch_head(client, url)
            if fetched is None:
                continue
            head, content_type, landing = fetched
            # A redirect chain can leave the public internet, and a docs host
            # that forwards /llms.txt to its landing page has answered "no".
            if not is_public_url(landing):
                continue
            if _looks_like_llms_txt(head, content_type):
                return url
    return None
