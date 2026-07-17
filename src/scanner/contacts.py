"""Extraction of maintainer contact channels from public sources.

Contacts are *personal data* even when every value here is self-published by
the maintainer on a public registry or profile. They are collected for
operational use — reaching the people responsible for a project — and are
deliberately kept out of the public report: see ``PUBLIC_REPORT_EXCLUDE`` in
the website backend, which strips ``data.contacts`` from the report endpoint.

Everything in this module is pure: callers pass payloads they have already
fetched, and get back ``ContactChannel`` lists. Nothing here performs I/O.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from .models import ContactChannel

# Deliberately conservative: this runs over registry metadata and Markdown
# prose, where a permissive pattern would harvest code samples and version
# strings. Requires a dotted TLD of at least two letters.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# npm/PyPI conventionally wrap the address: "Name <mail@example.com>".
_ANGLE_EMAIL_RE = re.compile(r"<\s*(" + _EMAIL_RE.pattern + r")\s*>")

# Addresses that reach nobody. GitHub's noreply forwards nowhere useful, and
# the rest are placeholders that appear in scaffolded manifests.
_UNROUTABLE_SUFFIXES = ("@users.noreply.github.com", "@noreply.github.com")
_UNROUTABLE_DOMAINS = {"example.com", "example.org", "example.net", "test.com", "localhost"}
_UNROUTABLE_LOCALPARTS = {"noreply", "no-reply", "donotreply", "do-not-reply"}

# project_urls / meta.links keys, lowercased, mapped to a contact role. Keys are
# free-form across registries, so match on substrings rather than exact names.
_URL_ROLE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("funding", "sponsor", "donate", "donation", "patreon", "opencollective", "tidelift"), "funding"),
    (("issue", "tracker", "bug", "report"), "issues"),
    (("chat", "discord", "slack", "gitter", "matrix", "irc", "forum", "discussion", "community"), "chat"),
    (("mailing list", "mailing-list", "mailinglist", "support", "contact", "help"), "support"),
)


def _is_routable(email: str) -> bool:
    lowered = email.lower()
    if lowered.endswith(_UNROUTABLE_SUFFIXES):
        return False
    local, _, domain = lowered.partition("@")
    if domain in _UNROUTABLE_DOMAINS:
        return False
    return local not in _UNROUTABLE_LOCALPARTS


def _emails_in(value: Any) -> list[str]:
    """Pull routable addresses out of a string, honouring "Name <addr>" form."""
    if not isinstance(value, str) or "@" not in value:
        return []
    angled = _ANGLE_EMAIL_RE.findall(value)
    found = angled or _EMAIL_RE.findall(value)
    return [e for e in found if _is_routable(e)]


def _role_for_url(label: str, url: str) -> Optional[str]:
    haystack = f"{label} {url}".lower()
    for needles, role in _URL_ROLE_HINTS:
        if any(needle in haystack for needle in needles):
            return role
    return None


def _channel(kind: str, value: str, role: str, source: str) -> Optional[ContactChannel]:
    value = value.strip()
    if not value:
        return None
    return ContactChannel(kind=kind, value=value, role=role, source=source)


def dedupe(channels: Iterable[Optional[ContactChannel]]) -> list[ContactChannel]:
    """Drop empties and repeats, preserving first-seen order.

    The same address legitimately arrives from several sources (a PyPI
    author_email that also appears in SECURITY.md); keep the first, which is
    the more structured source, since collectors run registry-first.
    """
    out: list[ContactChannel] = []
    seen: set[tuple[str, str, str]] = set()
    for channel in channels:
        if channel is None:
            continue
        key = (channel.kind, channel.value.lower(), channel.role)
        if key in seen:
            continue
        seen.add(key)
        out.append(channel)
    return out


def _from_url_map(links: Any, source: str) -> list[Optional[ContactChannel]]:
    """Map a registry's {label: url} dict onto funding/issues/chat/support."""
    if not isinstance(links, dict):
        return []
    out: list[Optional[ContactChannel]] = []
    for label, url in links.items():
        if not isinstance(url, str) or not isinstance(label, str):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        role = _role_for_url(label, url)
        if role:
            out.append(_channel("url", url, role, source))
    return out


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def from_owner_profile(raw: dict[str, Any]) -> list[ContactChannel]:
    """Contacts on a /users/{login} or /orgs/{login} payload.

    Both endpoints expose ``email`` and ``twitter_username`` for accounts that
    chose to publish them; the fields are simply absent otherwise.
    """
    source = "github-owner-profile"
    channels: list[Optional[ContactChannel]] = []
    for email in _emails_in(raw.get("email")):
        channels.append(_channel("email", email, "owner", source))
    twitter = raw.get("twitter_username")
    if isinstance(twitter, str) and twitter.strip():
        channels.append(_channel("handle", f"@{twitter.strip().lstrip('@')}", "owner", source))
    blog = raw.get("blog")
    if isinstance(blog, str) and blog.startswith(("http://", "https://")):
        channels.append(_channel("url", blog, "owner", source))
    return dedupe(channels)


def from_security_policy(text: str, path: str) -> list[ContactChannel]:
    """Contacts named in a SECURITY.md.

    A security policy's whole purpose is to publish a disclosure route, so the
    addresses and links in it are the intended contact path — but the file is
    prose, so this stays narrow: explicit addresses, and links only when the
    surrounding label marks them as a reporting channel.
    """
    channels: list[Optional[ContactChannel]] = []
    for email in _emails_in(text):
        channels.append(_channel("email", email, "security", path))

    # Markdown links whose text names a reporting route, e.g.
    # "[report a vulnerability](https://…/security/advisories/new)".
    for label, url in re.findall(r"\[([^\]]{0,80})\]\((https?://[^\s)]+)\)", text):
        haystack = f"{label} {url}".lower()
        if any(k in haystack for k in ("security", "advisor", "vulnerab", "disclos", "report")):
            channels.append(_channel("url", url, "security", path))
    return dedupe(channels)


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------


def from_pypi(payload: dict[str, Any]) -> list[ContactChannel]:
    info = payload.get("info") or {}
    channels: list[Optional[ContactChannel]] = []
    for field, role in (("author_email", "author"), ("maintainer_email", "maintainer")):
        for email in _emails_in(info.get(field)):
            channels.append(_channel("email", email, role, "pypi"))
    channels += _from_url_map(info.get("project_urls"), "pypi")
    return dedupe(channels)


def from_npm(payload: dict[str, Any]) -> list[ContactChannel]:
    channels: list[Optional[ContactChannel]] = []

    author = payload.get("author")
    if isinstance(author, dict):
        for email in _emails_in(author.get("email")):
            channels.append(_channel("email", email, "author", "npm"))
    else:
        # npm normalizes to a dict, but published packages still carry the
        # "Name <mail@example.com>" string form in older manifests.
        for email in _emails_in(author):
            channels.append(_channel("email", email, "author", "npm"))

    for maintainer in payload.get("maintainers") or []:
        if isinstance(maintainer, dict):
            values = [maintainer.get("email")]
        else:
            values = [maintainer]
        for value in values:
            for email in _emails_in(value):
                channels.append(_channel("email", email, "maintainer", "npm"))

    bugs = payload.get("bugs")
    if isinstance(bugs, dict):
        if isinstance(bugs.get("url"), str) and bugs["url"].startswith(("http://", "https://")):
            channels.append(_channel("url", bugs["url"], "issues", "npm"))
        for email in _emails_in(bugs.get("email")):
            channels.append(_channel("email", email, "issues", "npm"))
    elif isinstance(bugs, str) and bugs.startswith(("http://", "https://")):
        channels.append(_channel("url", bugs, "issues", "npm"))

    funding = payload.get("funding")
    entries = funding if isinstance(funding, list) else [funding]
    for entry in entries:
        url = entry.get("url") if isinstance(entry, dict) else entry
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            channels.append(_channel("url", url, "funding", "npm"))

    return dedupe(channels)


def from_rubygems(payload: dict[str, Any]) -> list[ContactChannel]:
    channels: list[Optional[ContactChannel]] = []
    for field, role in (
        ("bug_tracker_uri", "issues"),
        ("mailing_list_uri", "support"),
    ):
        url = payload.get(field)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            channels.append(_channel("url", url, role, "rubygems"))
    # Gem "authors" is a display string ("Jane Doe, John Roe"), not an address
    # field — but gems that publish one embed it in the angle-bracket form.
    for email in _emails_in(payload.get("authors")):
        channels.append(_channel("email", email, "author", "rubygems"))
    return dedupe(channels)


def from_hex(payload: dict[str, Any]) -> list[ContactChannel]:
    meta = payload.get("meta") or {}
    return dedupe(_from_url_map(meta.get("links"), "hex"))


def from_packagist(payload: dict[str, Any]) -> list[ContactChannel]:
    pkg = payload.get("package") or {}
    channels: list[Optional[ContactChannel]] = []
    for maintainer in pkg.get("maintainers") or []:
        if isinstance(maintainer, dict):
            for email in _emails_in(maintainer.get("email")):
                channels.append(_channel("email", email, "maintainer", "packagist"))
    return dedupe(channels)


def from_crates(payload: dict[str, Any]) -> list[ContactChannel]:
    """crates.io publishes no owner addresses on the public crate endpoint —
    only the crate's own links, which count when they name a channel."""
    crate = payload.get("crate") or {}
    links = {k: v for k, v in crate.items() if k in ("homepage", "documentation")}
    return dedupe(_from_url_map(links, "crates"))
