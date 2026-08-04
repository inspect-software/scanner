"""Which image identifies a repository, and where it came from.

The catalogue shows one small round mark per repository. This module decides
what that mark is. The decision is recorded, not just the result: every report
carries the source type and the exact URL, because a mark whose provenance is
"the owner's GitHub avatar" is a much weaker claim than one whose provenance is
"the icon the project's own website serves", and a reader who wonders why a
library is wearing a company logo deserves an answer.

Sources, best evidence first. The first candidate that survives validation wins:

``nuget``     The registry's own package icon. Of the nine registries this
              scanner supports, NuGet is the only one that publishes one — PyPI,
              npm, crates.io, Packagist, RubyGems, Hex, Go and Maven have no
              such field. Costs no request: the version is already known.
``homepage``  ``<link rel="apple-touch-icon">``, then the largest declared
              ``rel="icon"``, then ``/favicon.ico`` on the homepage the
              repository declares. This is the source that most often yields a
              real project mark.
``readme``    The first image in the README that is not a badge and not a
              photograph of a person. Costs no request — the README is already
              in the snapshot.
``tree``      A logo-ish file in the repository itself. Costs no request — the
              file tree is already in hand for the quality signals.
``avatar``    The owning account's GitHub avatar. Always available, and
              therefore the floor rather than a find: it identifies the
              *publisher*, and every repository under one account shares it.

Validation is where most of the work is. Each rule below exists because it was
observed producing a wrong mark against the production catalogue (see
``experiments/package-icons``):

* **Aspect ratio.** The usual ``logo.png`` in a repository is a horizontal
  wordmark — chalk's is 500x230 — which in a round slot letterboxes to an
  unreadable sliver. Anything wider than ``MAX_ASPECT`` is refused.
* **SVG dimensions come from the root element only.** Reading width/height from
  the whole document picks up whichever shape is declared last; on a
  shields-style badge that is its 14px square logo, which then measures as a
  perfect square. A pepy.tech download badge won as ``psf/requests``' icon this
  way.
* **Badges are not icons.** ``readme.badge_host`` decides this, so the badge
  vocabulary lives in one place.
* **Photographs are not icons.** Gravatar and GitHub's avatar CDN, including
  its numbered aliases (``avatars1.``), which older READMEs still use.
* **A homepage on a shared platform is not a project site.** A repository whose
  declared homepage is ``https://github.com/owner`` or an ``x.com`` status URL
  hands back that platform's favicon; both were seen in the top twenty
  repositories by download volume.
* **Stock icons of documentation hosts.** ``requests.readthedocs.io/favicon.ico``
  is byte-for-byte Read the Docs' own. These cannot be refused by host — a
  project on such a platform may well serve its own icon — so known stock icons
  are matched by content hash from ``data/platform_icon_hashes.json``.

Everything is fetched with :func:`fetch_candidate`, which refuses to talk to
anything but a public host: the URLs here are influenced by the repository
under audit, and an unguarded fetcher would happily read a cloud metadata
endpoint on its behalf.

The bytes themselves are not stored in the report — reports are JSON, and the
website caches the image separately from ``source_url``. What is stored is
enough to fetch it again and to know whether it changed: the URL, the media
type, the dimensions and the content hash.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from html import unescape
from pathlib import Path
from typing import Iterable, Optional, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from .models import EcosystemPackage, IconInfo, IconRejection, OwnerProfile
from .readme import _image_urls, badge_host

USER_AGENT = "inspect-scanner (+https://github.com/inspect-software/scanner)"

# Wider than this is a wordmark or a banner, not a mark that can be shown at
# 40px in a circle.
MAX_ASPECT = 1.35
# Below this there is no image worth rendering; favicons of 16px exist in
# quantity and look like mud at display size.
MIN_SIDE = 24
# An icon is a small file. Anything larger is a photograph, a screenshot, or a
# decompression bomb, and none of those belong in this slot.
MAX_BYTES = 2 * 1024 * 1024
# Only a page's head is read for icon links, so only that much is downloaded.
# Generous enough for the inflated <head> of a modern framework's output.
MAX_HTML_BYTES = 512 * 1024
# Candidates are cheap to list and expensive to fetch. Stop early.
MAX_CANDIDATES_PER_SOURCE = 5
# Rejections are recorded for the admin view, but a pathological README must
# not be able to grow the report without bound.
MAX_RECORDED_REJECTIONS = 8

_DATA = Path(__file__).parent / "data" / "platform_icon_hashes.json"


def _load_platform_hashes() -> dict[str, str]:
    try:
        return json.loads(_DATA.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - packaging accident
        return {}


PLATFORM_ICON_HASHES: dict[str, str] = _load_platform_hashes()


# ---------------------------------------------------------------------------
# Host classification
# ---------------------------------------------------------------------------

# The whole domain belongs to one company, so its favicon is that company's.
# A repository declaring one of these as its homepage has declared a profile
# page, a registry listing or a tweet — not a project site.
GENERIC_HOSTS = (
    "github.com", "gitlab.com", "bitbucket.org", "sourceforge.net",
    "x.com", "twitter.com", "t.co", "facebook.com", "linkedin.com", "medium.com",
    "npmjs.com", "npmjs.org", "pypi.org", "crates.io", "rubygems.org",
    "packagist.org", "nuget.org", "hex.pm", "pkg.go.dev", "mvnrepository.com",
    "readthedocs.org", "discord.gg", "google.com", "groups.google.com",
)

# Platforms that give each project a subdomain. Their *apex* is generic, but a
# project subdomain may serve either the project's icon or the platform's
# stock one — which is what the content-hash table separates.
SUBDOMAIN_PLATFORMS = (
    "readthedocs.io", "github.io", "gitbook.io", "netlify.app", "vercel.app",
    "pages.dev", "herokuapp.com", "surge.sh",
)

# Badge services `readme.BADGE_HOSTS` does not name. Kept here rather than
# added there because that list drives the reported badge counts, and widening
# it would silently move those figures.
EXTRA_BADGE_HOSTS = ("pepy.tech", "nodei.co", "deepsource.io", "badge.buildkite.com")

_PERSON_HOST = re.compile(
    r"(^|\.)(gravatar\.com|avatars\d*\.githubusercontent\.com)$", re.I
)


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().split(":")[0]


def is_generic_host(host: str) -> bool:
    """True for a host whose icon identifies the platform, not the project."""
    host = host.lower().split(":")[0]
    if any(host == g or host.endswith("." + g) for g in GENERIC_HOSTS):
        return True
    # The bare apex of a subdomain platform is the platform's own site.
    return any(host == p or host == "www." + p for p in SUBDOMAIN_PLATFORMS)


def is_badge(url: str, full_name: Optional[str] = None) -> bool:
    if badge_host(url, full_name):
        return True
    host = host_of(url)
    return any(host == b or host.endswith("." + b) for b in EXTRA_BADGE_HOSTS)


def is_person_image(url: str) -> bool:
    return bool(_PERSON_HOST.search(host_of(url)))


# ---------------------------------------------------------------------------
# Image sniffing: format and intrinsic size, from the header alone
# ---------------------------------------------------------------------------

_SVG_VIEWBOX = re.compile(
    rb"viewBox\s*=\s*[\"']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)"
)
_SVG_DIM = re.compile(rb"\b(width|height)\s*=\s*[\"']?\s*([\d.]+)")


def _svg_size(raw: bytes) -> Optional[tuple[float, float]]:
    """Intrinsic size of an SVG, read from the root element only.

    Reading the whole document instead takes the dimensions of whatever shape
    happens to be declared last, which on a badge is its small square logo.
    """
    end = raw.find(b">")
    root = raw[: end + 1] if 0 < end < 8192 else raw[:8192]
    box = _SVG_VIEWBOX.search(root)
    if box:
        return float(box.group(1)), float(box.group(2))
    dims: dict[str, float] = {}
    for key, value in _SVG_DIM.findall(root):
        dims.setdefault(key.decode(), float(value))
    if "width" in dims and "height" in dims:
        return dims["width"], dims["height"]
    return None


def _jpeg_size(raw: bytes) -> Optional[tuple[float, float]]:
    i = 2
    while i + 9 < len(raw):
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        # SOF0..SOF15, excluding the four that are not frame headers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(raw[i + 5 : i + 7], "big")
            width = int.from_bytes(raw[i + 7 : i + 9], "big")
            return float(width), float(height)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(raw[i + 2 : i + 4], "big")
    return None


def _webp_size(raw: bytes) -> Optional[tuple[float, float]]:
    chunk = raw[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(raw[24:27], "little") + 1
        height = int.from_bytes(raw[27:30], "little") + 1
        return float(width), float(height)
    if chunk == b"VP8 ":
        return (
            float(int.from_bytes(raw[26:28], "little") & 0x3FFF),
            float(int.from_bytes(raw[28:30], "little") & 0x3FFF),
        )
    if chunk == b"VP8L":
        bits = int.from_bytes(raw[21:25], "little")
        return float((bits & 0x3FFF) + 1), float(((bits >> 14) & 0x3FFF) + 1)
    return None


def _ico_size(raw: bytes) -> Optional[tuple[float, float]]:
    """Largest image in an ICO container. A 0 byte means 256."""
    count = int.from_bytes(raw[4:6], "little")
    best: Optional[tuple[float, float]] = None
    for index in range(min(count, 32)):
        entry = 6 + index * 16
        if entry + 2 > len(raw):
            break
        width = raw[entry] or 256
        height = raw[entry + 1] or 256
        if best is None or width * height > best[0] * best[1]:
            best = (float(width), float(height))
    return best


def sniff(raw: bytes) -> tuple[Optional[str], Optional[tuple[float, float]]]:
    """(media type, intrinsic size) of an image, from its header.

    Pure and dependency-free on purpose: the scanner needs the shape of an
    image to decide whether it can be used, not its pixels, and an image
    library is a large dependency to take on for a width and a height.
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", (
            float(int.from_bytes(raw[16:20], "big")),
            float(int.from_bytes(raw[20:24], "big")),
        )
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg", _jpeg_size(raw)
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", (
            float(int.from_bytes(raw[6:8], "little")),
            float(int.from_bytes(raw[8:10], "little")),
        )
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", _webp_size(raw)
    if raw[:4] == b"\x00\x00\x01\x00":
        return "image/vnd.microsoft.icon", _ico_size(raw)
    head = raw[:512].lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in raw[:4096]):
        return "image/svg+xml", _svg_size(raw)
    return None, None


def validate(raw: bytes) -> tuple[Optional[IconInfo], Optional[str]]:
    """Judge fetched bytes as an icon. Returns (info, rejection reason)."""
    if not raw:
        return None, "empty response"
    if len(raw) > MAX_BYTES:
        return None, f"too large ({len(raw)} bytes)"
    media_type, size = sniff(raw)
    if media_type is None:
        return None, "not a recognized image format"
    digest = hashlib.sha256(raw).hexdigest()
    platform = PLATFORM_ICON_HASHES.get(digest)
    if platform:
        return None, f"stock icon of {platform}"
    if size is None:
        # An SVG with neither viewBox nor width/height could be any shape at
        # all, and the aspect rule is the main defence against banners.
        return None, "dimensions undeclared"
    width, height = size
    if min(width, height) < MIN_SIDE:
        return None, f"too small ({width:g}x{height:g})"
    if max(width, height) / max(1e-6, min(width, height)) > MAX_ASPECT:
        return None, f"not square enough ({width:g}x{height:g})"
    return (
        IconInfo(
            collected=True,
            media_type=media_type,
            width=int(width),
            height=int(height),
            bytes=len(raw),
            content_hash=digest,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Candidate sources. The pure ones take already-collected data and cost nothing.
# ---------------------------------------------------------------------------

def nuget_candidates(packages: Sequence[EcosystemPackage]) -> list[str]:
    """NuGet's flat-container icon endpoint, for each published package."""
    urls = []
    for pkg in packages:
        if pkg.ecosystem != "nuget" or not pkg.exists or not pkg.latest_version:
            continue
        urls.append(
            "https://api.nuget.org/v3-flatcontainer/"
            f"{pkg.name.lower()}/{pkg.latest_version.lower()}/icon"
        )
    return urls[:MAX_CANDIDATES_PER_SOURCE]


_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_SIZES = re.compile(r"(\d+)x(\d+)")


def _attr(tag: str, name: str) -> str:
    """One attribute's value, quoted or not, with entities decoded.

    Two things that look like pedantry and are not. Unquoted attributes are
    emitted by minifying static-site generators — numpy.org serves
    ``<link rel=icon href=/images/favicon.ico>``, which a quotes-only pattern
    reads as no icon at all. And an href carrying query parameters arrives
    HTML-escaped: GitBook's icon URL is one long ``?url=…&amp;width=180`` that
    fetches as a 404 unless the entities are decoded first.
    """
    match = re.search(
        r"\b" + name + r"""\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""", tag, re.I
    )
    if not match:
        return ""
    return unescape(next((group for group in match.groups() if group is not None), ""))


def page_icon_candidates(html: str, base_url: str) -> list[str]:
    """Icons a page declares, best first, then its ``/favicon.ico``.

    ``apple-touch-icon`` ranks first because it is the one link type that is
    reliably square, opaque and large; a bare ``rel="icon"`` is often 16px.
    """
    ranked: list[tuple[int, str]] = []
    for tag in _LINK_TAG.findall(html):
        rel = _attr(tag, "rel").lower()
        href = _attr(tag, "href")
        if "icon" not in rel or not href:
            continue
        sizes = _SIZES.match(_attr(tag, "sizes").strip().lower())
        rank = 1000 if "apple" in rel else (int(sizes.group(1)) if sizes else 0)
        ranked.append((rank, urljoin(base_url, href)))
    ranked.sort(key=lambda item: -item[0])
    urls = [url for _, url in ranked]
    urls.append(urljoin(base_url, "/favicon.ico"))
    seen: set[str] = set()
    unique = [u for u in urls if not (u in seen or seen.add(u))]
    return unique[:MAX_CANDIDATES_PER_SOURCE]


# Images a README shows before it has said anything: the project's own logo
# lives here when it exists at all.
_README_SCAN_LIMIT = 12


def readme_candidates(
    readme_text: Optional[str], full_name: str, default_branch: Optional[str]
) -> list[str]:
    """README images that could be a project mark, in document order."""
    if not readme_text:
        return []
    branch = default_branch or "HEAD"
    urls: list[str] = []
    for _, url in _image_urls(readme_text)[:_README_SCAN_LIMIT]:
        if url.startswith("//"):
            url = "https:" + url
        elif not url.lower().startswith(("http://", "https://")):
            path = url.lstrip("./")
            if not path:
                continue
            url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"
        if is_badge(url, full_name) or is_person_image(url):
            continue
        urls.append(url)
        if len(urls) >= MAX_CANDIDATES_PER_SOURCE:
            break
    return urls


_LOGO_FILE = re.compile(
    r"(^|/)(logo|icon|mark|logomark|brand)[-_a-z0-9]*\.(svg|png|webp)$", re.I
)
# Directories a project's own artwork lives in. Anything deeper is a vendored
# asset, a test fixture or a third-party logo in a documentation page.
_ART_DIRS = re.compile(
    r"^(|docs?/|assets?/|\.github/|images?/|img/|static/|media/|art/|branding/|"
    r"resources/|www/|web/|site/|public/|docs?/(assets?|images?|img|static)/)$",
    re.I,
)


def _tree_rank(path: str) -> int:
    name = path.rsplit("/", 1)[-1].lower()
    rank = 0
    # "icon" and "logomark" name a square mark; "logo" alone is usually the
    # horizontal lockup, which the aspect rule will refuse anyway.
    rank += 40 if name.startswith(("icon", "logomark", "mark")) else 0
    rank -= 25 * path.count("/")
    # Dark/light variants are alternates, not the default mark.
    rank -= 30 if re.search(r"\b(dark|inverse|white|mono)\b", name) else 0
    rank += 10 if name.endswith(".svg") else 0
    return rank


def tree_candidates(
    tree_paths: Iterable[str], full_name: str, default_branch: Optional[str]
) -> list[str]:
    """Logo-ish files in the repository, most likely first."""
    branch = default_branch or "HEAD"
    hits = []
    for path in tree_paths:
        if not _LOGO_FILE.search(path):
            continue
        directory = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        if not _ART_DIRS.match(directory):
            continue
        hits.append((_tree_rank(path), path))
    hits.sort(key=lambda hit: -hit[0])
    return [
        f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"
        for _, path in hits[:MAX_CANDIDATES_PER_SOURCE]
    ]


def avatar_candidates(owner: Optional[OwnerProfile]) -> list[str]:
    """The owning account's avatar, at a size worth rendering."""
    if owner is None or not owner.avatar_url:
        return []
    url = owner.avatar_url
    return [f"{url}{'&' if '?' in url else '?'}s=256"]


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def is_public_url(url: str) -> bool:
    """True when a URL points at a public host over http(s).

    Candidate URLs derive from the repository under audit — its declared
    homepage, its README — so they are attacker-influenced input. Without this
    check a repository could point the scanner at a cloud metadata endpoint or
    an internal service and have it fetched from inside the network.

    Known limit: the name is resolved here and again by the HTTP client, so a
    host that answers with a public address on the first lookup and a private
    one on the second (DNS rebinding) is not stopped. Closing that needs the
    connection pinned to the address this function vetted, which httpx does not
    expose. What is guarded is the ordinary case — a literal private address, a
    metadata IP, a name that simply resolves inward — and the redirect chain,
    which is re-checked at the landing URL.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except OSError:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address.is_global or address.is_multicast:
            return False
    return bool(infos)


def get_capped(
    client: httpx.Client,
    url: str,
    *,
    limit: int = MAX_BYTES,
    accept: str = "image/*,*/*;q=0.5",
) -> tuple[Optional[bytes], Optional[str], Optional[httpx.Response]]:
    """GET a URL, refusing to buffer more than ``limit`` bytes.

    Streamed rather than fetched whole, because these URLs belong to the
    repository under audit. ``httpx.get`` materializes the entire body before
    any caller can measure it — measured: a 26 MB response arrived in full
    against a 2 MB cap — so a repository could point at an arbitrarily large
    "favicon" and exhaust the scanner's memory. Reading in chunks and stopping
    at the cap is what makes MAX_BYTES a limit rather than a description.
    """
    try:
        with client.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0, connect=8.0),
            headers={"User-Agent": USER_AGENT, "Accept": accept},
        ) as response:
            if response.status_code != 200:
                return None, f"HTTP {response.status_code}", response
            # Trust a declared length only to refuse early; a lying or absent
            # header still meets the real check below.
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                return None, f"too large ({declared} bytes)", response
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    return None, f"too large (over {limit} bytes)", response
            return bytes(body), None, response
    except httpx.HTTPError as exc:
        return None, f"request failed ({type(exc).__name__})", None


def fetch_candidate(client: httpx.Client, url: str) -> tuple[Optional[bytes], Optional[str]]:
    """Fetch one candidate. Returns (body, rejection reason)."""
    if not is_public_url(url):
        return None, "not a public http(s) URL"
    body, reason, response = get_capped(client, url)
    if response is None:
        return None, reason
    # A redirect chain can leave the public internet even when the first hop
    # was fine, so the landing URL is checked too.
    if not is_public_url(str(response.url)):
        return None, "redirected to a non-public host"
    if body is None:
        return None, reason
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    if content_type.startswith("text/html"):
        return None, "served HTML, not an image"
    return body, None


def fetch_page_icons(client: httpx.Client, homepage: str) -> list[str]:
    """Icon URLs declared by a repository's homepage."""
    url = homepage.strip()
    if not url:
        return []
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    if is_generic_host(host_of(url)) or not is_public_url(url):
        return []
    # Only the head of the document is ever read, so only the head is
    # downloaded — `<link rel=icon>` lives there, and a homepage is as
    # attacker-controlled as an icon URL.
    body, _, response = get_capped(client, url, limit=MAX_HTML_BYTES, accept="text/html,*/*")
    if body is None or response is None:
        return []
    # A project site forwarded to a GitHub repository page is still a GitHub
    # page, and unmaintained libraries do this often.
    if is_generic_host(host_of(str(response.url))) or not is_public_url(str(response.url)):
        return []
    return page_icon_candidates(body.decode("utf-8", "replace"), str(response.url))


def collect_icon(
    client: httpx.Client,
    *,
    full_name: str,
    owner: Optional[OwnerProfile],
    homepage: Optional[str],
    readme_text: Optional[str],
    tree_paths: Sequence[str],
    packages: Sequence[EcosystemPackage],
    default_branch: Optional[str],
    warnings: Optional[list[str]] = None,
) -> IconInfo:
    """Walk the cascade and return the first candidate that validates.

    Never raises and never aborts a scan: an unreachable homepage or a
    malformed image is a rejection recorded against that candidate, and the
    cascade moves on. The worst case is ``source_type="avatar"``, which is
    always available.
    """
    sources: list[tuple[str, list[str]]] = [
        ("nuget", nuget_candidates(packages)),
        ("homepage", fetch_page_icons(client, homepage) if homepage else []),
        ("readme", readme_candidates(readme_text, full_name, default_branch)),
        ("tree", tree_candidates(tree_paths, full_name, default_branch)),
        ("avatar", avatar_candidates(owner)),
    ]

    rejections: list[IconRejection] = []
    considered = 0
    for source_type, candidates in sources:
        for url in candidates:
            considered += 1
            raw, reason = fetch_candidate(client, url)
            if raw is not None:
                info, reason = validate(raw)
                if info is not None:
                    info.source_type = source_type
                    info.source_url = url
                    info.candidates_considered = considered
                    info.rejected = rejections[:MAX_RECORDED_REJECTIONS]
                    return info
            if len(rejections) < MAX_RECORDED_REJECTIONS:
                rejections.append(
                    IconRejection(source_type=source_type, url=url, reason=reason or "rejected")
                )

    if warnings is not None and owner is not None and owner.avatar_url:
        # Reaching here means even the avatar failed, which is a network
        # problem rather than a property of the repository.
        warnings.append("No repository icon could be resolved, including the owner avatar")
    return IconInfo(
        collected=True,
        candidates_considered=considered,
        rejected=rejections[:MAX_RECORDED_REJECTIONS],
    )
