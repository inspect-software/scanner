"""Tests for icon resolution.

Every rejection rule here was written because a real repository produced a
wrong mark without it, so each test names the case it protects.
"""

from __future__ import annotations

import hashlib
import struct
import zlib

import pytest

from scanner import icon
from scanner.models import EcosystemPackage, OwnerProfile


# ---------------------------------------------------------------------------
# builders for real image bytes — the sniffer reads headers, so these must be
# genuine files rather than fixtures with the right length
# ---------------------------------------------------------------------------

def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 10


def ico(width: int, height: int) -> bytes:
    # width/height of 256 are stored as 0.
    return (
        b"\x00\x00\x01\x00"
        + struct.pack("<H", 1)
        + bytes([width % 256, height % 256, 0, 0])
        + b"\x00" * 12
    )


def webp_vp8x(width: int, height: int) -> bytes:
    body = b"VP8X" + b"\x00" * 8
    raw = bytearray(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + body + b"\x00" * 16)
    raw[24:27] = (width - 1).to_bytes(3, "little")
    raw[27:30] = (height - 1).to_bytes(3, "little")
    return bytes(raw)


def svg(attrs: str) -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}><rect width="14" height="14"/></svg>'.encode()


# ---------------------------------------------------------------------------
# sniffing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected_type, expected_size",
    [
        (png(120, 120), "image/png", (120.0, 120.0)),
        (gif(64, 64), "image/gif", (64.0, 64.0)),
        (ico(32, 32), "image/vnd.microsoft.icon", (32.0, 32.0)),
        (webp_vp8x(96, 96), "image/webp", (96.0, 96.0)),
        (svg('viewBox="0 0 48 48"'), "image/svg+xml", (48.0, 48.0)),
        (svg('width="64" height="64"'), "image/svg+xml", (64.0, 64.0)),
    ],
)
def test_sniff_reads_format_and_size_from_the_header(raw, expected_type, expected_size):
    assert icon.sniff(raw) == (expected_type, expected_size)


def test_an_ico_stores_256_as_zero():
    assert icon.sniff(ico(0, 0))[1] == (256.0, 256.0)


def test_unknown_bytes_are_not_an_image():
    assert icon.sniff(b"just some text, honestly") == (None, None)


def test_svg_size_comes_from_the_root_element_only():
    """A shields-style badge declares a 14px square logo inside a 134x20 strip.

    Reading the last width/height in the document measures the logo and the
    badge passes as a perfect square — this is how a pepy.tech download badge
    became psf/requests' icon.
    """
    badge = svg('width="134" height="20"')
    assert icon.sniff(badge)[1] == (134.0, 20.0)
    assert icon.validate(badge)[0] is None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_a_square_png_validates_and_is_hashed():
    info, reason = icon.validate(png(128, 128))
    assert reason is None
    assert info.media_type == "image/png"
    assert (info.width, info.height) == (128, 128)
    assert info.content_hash == hashlib.sha256(png(128, 128)).hexdigest()


def test_a_wordmark_is_refused():
    """chalk's media/logo.svg is 500x230 and letterboxes to nothing."""
    info, reason = icon.validate(svg('viewBox="0 0 500 230"'))
    assert info is None
    assert "square" in reason


def test_a_tiny_favicon_is_refused():
    info, reason = icon.validate(png(16, 16))
    assert info is None
    assert "too small" in reason


def test_an_svg_of_unknown_shape_is_refused():
    """No viewBox and no width/height means the aspect rule cannot apply."""
    info, reason = icon.validate(b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    assert info is None
    assert reason == "dimensions undeclared"


def test_a_platform_stock_icon_is_refused(monkeypatch):
    """requests.readthedocs.io/favicon.ico is byte-for-byte Read the Docs'."""
    raw = png(64, 64)
    monkeypatch.setitem(
        icon.PLATFORM_ICON_HASHES, hashlib.sha256(raw).hexdigest(), "Read the Docs"
    )
    info, reason = icon.validate(raw)
    assert info is None
    assert reason == "stock icon of Read the Docs"


def test_the_shipped_stock_icon_table_is_populated():
    """A packaging accident that empties this file must not pass silently."""
    assert len(icon.PLATFORM_ICON_HASHES) >= 10
    assert "Read the Docs" in icon.PLATFORM_ICON_HASHES.values()


def test_an_oversized_response_is_refused():
    info, reason = icon.validate(b"\x89PNG\r\n\x1a\n" + b"\x00" * (icon.MAX_BYTES + 1))
    assert info is None
    assert "too large" in reason


# ---------------------------------------------------------------------------
# host classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host, generic",
    [
        ("github.com", True),          # micromatch/picomatch declares this
        ("twitter.com", True),         # isaacs/minipass declares a tweet
        ("www.npmjs.com", True),       # vercel/ms points at its own npm page
        ("readthedocs.io", True),      # the platform's apex
        ("requests.readthedocs.io", False),   # a project subdomain may be its own
        ("numpy.org", False),
        ("proxy-agents.n8.io", False),
    ],
)
def test_generic_hosts_are_recognized(host, generic):
    assert icon.is_generic_host(host) is generic


def test_person_images_are_recognized_including_numbered_avatar_aliases():
    assert icon.is_person_image("https://gravatar.com/avatar/abc")
    assert icon.is_person_image("https://avatars.githubusercontent.com/u/1?v=4")
    # micromark's README uses the numbered alias, which a plain suffix check misses.
    assert icon.is_person_image("https://avatars1.githubusercontent.com/u/14985020?s=256")
    assert not icon.is_person_image("https://numpy.org/images/favicon.ico")


def test_badge_hosts_cover_services_the_readme_module_does_not_name():
    assert icon.is_badge("https://static.pepy.tech/badge/requests/month")
    assert icon.is_badge("https://img.shields.io/badge/build-passing-green.svg")
    assert not icon.is_badge("https://numpy.org/images/favicon.ico")


# ---------------------------------------------------------------------------
# candidate ranking (all pure)
# ---------------------------------------------------------------------------

def test_page_icons_prefer_apple_touch_then_the_largest_declared_size():
    html = """
      <link rel="icon" sizes="32x32" href="/small.png">
      <link rel="apple-touch-icon" sizes="180x180" href="/touch.png">
      <link rel="icon" sizes="192x192" href="/large.png">
    """
    assert icon.page_icon_candidates(html, "https://example.org/") == [
        "https://example.org/touch.png",
        "https://example.org/large.png",
        "https://example.org/small.png",
        "https://example.org/favicon.ico",
    ]


def test_unquoted_attributes_are_read():
    """numpy.org serves <link rel=icon href=/images/favicon.ico>."""
    html = "<link rel=icon href=/images/favicon.ico>"
    assert icon.page_icon_candidates(html, "https://numpy.org/")[0] == (
        "https://numpy.org/images/favicon.ico"
    )


def test_html_entities_in_an_href_are_decoded():
    """GitBook's icon href is a query string full of &amp;."""
    html = '<link rel="apple-touch-icon" href="/img?url=x&amp;width=180">'
    assert icon.page_icon_candidates(html, "https://example.org/")[0] == (
        "https://example.org/img?url=x&width=180"
    )


def test_a_page_with_no_icon_links_still_offers_favicon_ico():
    assert icon.page_icon_candidates("<html></html>", "https://example.org/docs/") == [
        "https://example.org/favicon.ico"
    ]


def test_readme_candidates_skip_badges_and_photographs_and_resolve_relative_paths():
    readme = (
        "[![build](https://img.shields.io/badge/ci-ok-green)](https://ci.example)\n"
        "![author](https://gravatar.com/avatar/abc)\n"
        "![logo](./media/logo-square.png)\n"
    )
    assert icon.readme_candidates(readme, "acme/widget", "main") == [
        "https://raw.githubusercontent.com/acme/widget/main/media/logo-square.png"
    ]


def test_readme_candidates_are_empty_without_a_readme():
    assert icon.readme_candidates(None, "acme/widget", "main") == []


def test_tree_candidates_rank_square_marks_above_deep_variants():
    paths = [
        "docs/assets/logo-dark.svg",
        "logo.png",
        "icon.svg",
        "vendor/other/logo.png",     # not an art directory
        "src/components/logo.tsx",   # not an image
    ]
    urls = icon.tree_candidates(paths, "acme/widget", "main")
    assert urls[0].endswith("/icon.svg")
    assert not any("vendor/" in url for url in urls)
    assert not any(url.endswith(".tsx") for url in urls)


def test_nuget_candidates_use_the_version_already_collected():
    packages = [
        EcosystemPackage(
            ecosystem="nuget", name="Newtonsoft.Json",
            registry_url="https://www.nuget.org/packages/Newtonsoft.Json",
            latest_version="13.0.3",
        ),
        EcosystemPackage(ecosystem="npm", name="chalk", registry_url="https://npmjs.com/chalk"),
    ]
    assert icon.nuget_candidates(packages) == [
        "https://api.nuget.org/v3-flatcontainer/newtonsoft.json/13.0.3/icon"
    ]


def test_avatar_candidate_asks_for_a_renderable_size():
    owner = OwnerProfile(login="acme", type="Organization",
                         avatar_url="https://avatars.githubusercontent.com/u/1?v=4")
    assert icon.avatar_candidates(owner) == [
        "https://avatars.githubusercontent.com/u/1?v=4&s=256"
    ]
    assert icon.avatar_candidates(None) == []


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://127.0.0.1:8000/",
        "http://localhost/",
        "http://10.0.0.5/icon.png",
        "file:///etc/passwd",
        "ftp://example.org/icon.png",
    ],
)
def test_non_public_urls_are_refused(url):
    assert icon.is_public_url(url) is False


def test_a_public_https_url_is_allowed():
    assert icon.is_public_url("https://numpy.org/images/favicon.ico") is True


# ---------------------------------------------------------------------------
# download cap
# ---------------------------------------------------------------------------

class _CappedStream:
    """Streams `total` bytes in chunks, counting what was actually read."""

    def __init__(self, total: int, chunk: int = 65536, headers: dict | None = None):
        self.total = total
        self.chunk = chunk
        self.status_code = 200
        self.url = "https://evil.example/icon.png"
        self.headers = headers if headers is not None else {"content-type": "image/png"}
        self.read = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        while self.read < self.total:
            step = min(self.chunk, self.total - self.read)
            self.read += step
            yield b"\x00" * step


class _StreamClient:
    def __init__(self, response):
        self.response = response

    def stream(self, method, url, **kwargs):
        return self.response


def test_a_huge_response_stops_at_the_cap_instead_of_buffering_it():
    """httpx.get materializes the whole body before any caller can measure it:
    measured at 26 MB against a 2 MB cap. The stream must stop early."""
    response = _CappedStream(total=64 * 1024 * 1024)
    body, reason, _ = icon.get_capped(_StreamClient(response), "https://evil.example/icon.png")

    assert body is None
    assert "too large" in reason
    # Read enough to know it was over, and then stopped — not the whole 64 MB.
    assert response.read <= icon.MAX_BYTES + 65536


def test_a_declared_oversize_length_is_refused_before_reading_anything():
    response = _CappedStream(
        total=64 * 1024 * 1024,
        headers={"content-type": "image/png", "content-length": str(64 * 1024 * 1024)},
    )
    body, reason, _ = icon.get_capped(_StreamClient(response), "https://evil.example/icon.png")

    assert body is None and "too large" in reason
    assert response.read == 0


def test_a_normal_image_streams_through_intact():
    raw = png(128, 128)

    class _Exact(_CappedStream):
        def iter_bytes(self):
            self.read = len(raw)
            yield raw

    body, reason, _ = icon.get_capped(
        _StreamClient(_Exact(len(raw))), "https://example.org/icon.png"
    )
    assert reason is None and body == raw


# ---------------------------------------------------------------------------
# the cascade
# ---------------------------------------------------------------------------

class FakeClient:
    """Serves canned bytes per URL; anything unlisted 404s."""

    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies
        self.requested: list[str] = []

    def stream(self, method, url, **kwargs):
        self.requested.append(url)
        body = self.bodies.get(url)
        return _FakeResponse(url, 200 if body is not None else 404, body or b"")


class _FakeResponse:
    """Matches the streaming interface `icon.get_capped` uses."""

    def __init__(self, url, status_code, content):
        self.url = url
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "image/png"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self.content


@pytest.fixture
def anywhere_is_public(monkeypatch):
    monkeypatch.setattr(icon, "is_public_url", lambda url: url.startswith("https://"))


def test_the_cascade_stops_at_the_first_candidate_that_validates(anywhere_is_public):
    logo = "https://raw.githubusercontent.com/acme/widget/main/icon.svg"
    client = FakeClient({logo: svg('viewBox="0 0 64 64"')})
    info = icon.collect_icon(
        client, full_name="acme/widget", owner=None, homepage=None,
        readme_text=None, tree_paths=["icon.svg"], packages=[], default_branch="main",
    )
    assert info.source_type == "tree"
    assert info.source_url == logo
    assert info.width == 64


def test_a_repository_with_nothing_of_its_own_falls_back_to_the_owner_avatar(anywhere_is_public):
    """The top twenty repositories by download volume all land here."""
    avatar = "https://avatars.githubusercontent.com/u/1?v=4&s=256"
    owner = OwnerProfile(login="chalk", type="Organization",
                         avatar_url="https://avatars.githubusercontent.com/u/1?v=4")
    client = FakeClient({avatar: png(256, 256)})
    info = icon.collect_icon(
        client, full_name="chalk/ansi-regex", owner=owner, homepage=None,
        readme_text=None, tree_paths=[], packages=[], default_branch="main",
    )
    assert info.source_type == "avatar"
    assert info.source_url == avatar


def test_rejections_are_recorded_so_the_choice_can_be_explained(anywhere_is_public):
    wordmark = "https://raw.githubusercontent.com/acme/widget/main/logo.svg"
    avatar = "https://avatars.githubusercontent.com/u/1?v=4&s=256"
    owner = OwnerProfile(login="acme", type="Organization",
                         avatar_url="https://avatars.githubusercontent.com/u/1?v=4")
    client = FakeClient({wordmark: svg('viewBox="0 0 500 230"'), avatar: png(256, 256)})
    info = icon.collect_icon(
        client, full_name="acme/widget", owner=owner, homepage=None,
        readme_text=None, tree_paths=["logo.svg"], packages=[], default_branch="main",
    )
    assert info.source_type == "avatar"
    assert [(r.source_type, "square" in r.reason) for r in info.rejected] == [("tree", True)]


def test_nothing_at_all_yields_an_empty_icon_not_an_exception(anywhere_is_public):
    info = icon.collect_icon(
        FakeClient({}), full_name="acme/widget", owner=None, homepage=None,
        readme_text=None, tree_paths=[], packages=[], default_branch=None,
    )
    assert info.collected is True
    assert info.source_type is None
    assert info.source_url is None
