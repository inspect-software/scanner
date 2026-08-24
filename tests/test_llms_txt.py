"""The website probe for llms.txt.

The repository tree is not where llms.txt usually lives: documentation
toolchains build it at docs-build time and serve it from the docs site
(locustio/locust does exactly this — llms.txt at docs.locust.io, nothing in
the tree). These tests cover which sites are considered worth probing, what
counts as a real llms.txt rather than a soft-404, and the probe end to end.
"""

import httpx
import pytest

from scanner import llms_txt
from scanner.llms_txt import candidate_bases, probe_llms_txt, _looks_like_llms_txt

LLMS = b"# Locust\n\n> Load testing tool\n\n## Docs\n- [Guide](https://docs.locust.io/)\n"


# ---------------------------------------------------------------------------
# candidate bases
# ---------------------------------------------------------------------------

def test_the_declared_homepage_is_probed_even_when_it_does_not_look_like_docs():
    assert candidate_bases("https://locust.cloud", None) == ["https://locust.cloud"]


def test_a_bare_domain_homepage_is_normalized_to_https():
    assert candidate_bases("example.org", None) == ["https://example.org"]


def test_a_homepage_on_a_generic_platform_nominates_nothing():
    assert candidate_bases("https://github.com/acme/widget", None) == []


def test_readme_links_qualify_only_when_they_look_like_documentation():
    readme = (
        "See https://docs.locust.io/en/stable/ for docs, "
        "chat at https://slack.example.com/join, "
        "and the site at https://locust.io/pricing."
    )
    assert candidate_bases(None, readme) == ["https://docs.locust.io"]


def test_a_readthedocs_subdomain_qualifies_by_suffix():
    assert candidate_bases(None, "https://widget.readthedocs.io/en/latest/") == [
        "https://widget.readthedocs.io"
    ]


def test_a_github_io_project_page_keeps_its_project_path_segment():
    assert candidate_bases(None, "https://acme.github.io/widget/guide.html") == [
        "https://acme.github.io/widget"
    ]


def test_a_docs_path_on_the_project_site_nominates_the_site_root():
    assert candidate_bases(None, "https://widget.dev/docs/getting-started") == [
        "https://widget.dev"
    ]


def test_bases_are_deduplicated_and_capped():
    readme = " ".join(
        f"https://docs.example{i}.org/guide https://docs.example{i}.org/api"
        for i in range(5)
    )
    bases = candidate_bases("https://docs.example0.org", readme)
    assert bases == [
        "https://docs.example0.org",
        "https://docs.example1.org",
        "https://docs.example2.org",
    ]


# ---------------------------------------------------------------------------
# what counts as an llms.txt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "head, content_type, verdict",
    [
        (LLMS, "text/plain", True),
        (LLMS, "text/markdown", True),
        (b"\xef\xbb\xbf# BOM first\n", "text/plain", True),
        # An SPA fallback serves its index page as a 200 for any path.
        (b"<!DOCTYPE html><html>...</html>", "text/plain", False),
        (LLMS, "text/html", False),
        (b"", "text/plain", False),
        # A soft-404: 200, plain text, no Markdown heading anywhere.
        (b"Not Found", "text/plain", False),
    ],
)
def test_shape_check_separates_markdown_from_html_and_soft_404s(head, content_type, verdict):
    assert _looks_like_llms_txt(head, content_type) is verdict


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------

class FakeClient(httpx.Client):
    """Serves canned (body, content-type) per URL; anything unlisted 404s.

    A real client over MockTransport, not a hand-written double. The double it
    replaced implemented only ``.stream()``, so no test here could express a
    redirect — and this probe follows redirects, which is where the guard that
    keeps it on the public internet has to act.
    """

    def __init__(self, bodies, redirects=None):
        self.bodies = dict(bodies)
        self.redirects = dict(redirects or {})
        self.requested = []
        super().__init__(transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        url = str(request.url)
        self.requested.append(url)
        if url in self.redirects:
            return httpx.Response(302, headers={"location": self.redirects[url]})
        entry = self.bodies.get(url)
        if entry is None:
            return httpx.Response(404, headers={"content-type": "text/html"})
        body, content_type = entry
        return httpx.Response(200, content=body, headers={"content-type": content_type})


@pytest.fixture
def anywhere_is_public(monkeypatch):
    """Public means anything https, in both modules.

    ``probe_llms_txt`` vets the candidate itself, but the per-hop check lives in
    ``icon.public_stream``; patching only this module would leave the fetch
    resolving real DNS.
    """
    from scanner import icon

    allow = lambda url: url.startswith("https://")
    monkeypatch.setattr(llms_txt, "is_public_url", allow)
    monkeypatch.setattr(icon, "is_public_url", allow)


def test_the_locust_shape_homepage_misses_docs_site_hits(anywhere_is_public):
    """Homepage 404s; the docs site the README links to serves the file."""
    client = FakeClient({"https://docs.locust.io/llms.txt": (LLMS, "text/plain")})
    url = probe_llms_txt(
        client, "https://locust.cloud", "Docs live at https://docs.locust.io/en/stable/."
    )
    assert url == "https://docs.locust.io/llms.txt"


def test_a_site_publishing_only_the_full_variant_still_counts(anywhere_is_public):
    client = FakeClient({"https://docs.widget.dev/llms-full.txt": (LLMS, "text/plain")})
    assert (
        probe_llms_txt(client, None, "https://docs.widget.dev/")
        == "https://docs.widget.dev/llms-full.txt"
    )


def test_a_docs_host_that_soft_404s_with_html_is_a_miss(anywhere_is_public):
    client = FakeClient(
        {
            "https://docs.widget.dev/llms.txt": (b"<!DOCTYPE html><html>", "text/html"),
            "https://docs.widget.dev/llms-full.txt": (b"<!DOCTYPE html><html>", "text/html"),
        }
    )
    assert probe_llms_txt(client, None, "https://docs.widget.dev/") is None


def test_nothing_nominated_probes_nothing():
    client = FakeClient({})
    assert probe_llms_txt(client, None, None) is None
    assert client.requested == []


def test_a_non_public_candidate_is_never_fetched(monkeypatch):
    monkeypatch.setattr(llms_txt, "is_public_url", lambda url: False)
    client = FakeClient({"https://docs.widget.dev/llms.txt": (LLMS, "text/plain")})
    assert probe_llms_txt(client, None, "https://docs.widget.dev/") is None
    assert client.requested == []


def test_a_docs_host_redirecting_inward_is_not_fetched(anywhere_is_public):
    """A homepage is repository-declared, so its redirects are too."""
    from scanner import icon

    private = "http://127.0.0.1:9/latest/meta-data/"
    probe = "https://docs.example.org/llms.txt"
    client = FakeClient({private: (LLMS, "text/plain")}, redirects={probe: private})
    # Loopback is not public even under the permissive fixture.
    icon.is_public_url = lambda url: url.startswith("https://")

    assert probe_llms_txt(client, "https://docs.example.org", None) is None
    assert private not in client.requested
