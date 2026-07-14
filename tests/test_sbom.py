"""Tests for the resolved dependency graph (GitHub SBOM) collection."""

from __future__ import annotations

import httpx

from scanner import sbom
from scanner.github import GitHubClient
from scanner.models import Dependency
from scanner.sbom import (
    collect_all_dependencies,
    parse_github_sbom,
    parse_purl,
    resolve_dependencies,
)


def _dep(ecosystem: str, name: str) -> Dependency:
    return Dependency(ecosystem=ecosystem, name=name, manifest="x")


def _client(responder) -> GitHubClient:
    gh = GitHubClient(token=[])
    gh._client = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(responder)
    )
    return gh


# ---------------------------------------------------------------------------
# parse_purl
# ---------------------------------------------------------------------------


def test_parse_purl_basic():
    assert parse_purl("pkg:npm/express@4.17.1") == ("npm", "express", "4.17.1")
    assert parse_purl("pkg:pypi/requests@2.31.0") == ("pypi", "requests", "2.31.0")
    assert parse_purl("pkg:cargo/serde@1.0.0") == ("crates", "serde", "1.0.0")
    assert parse_purl("pkg:composer/monolog/monolog@3.0.0") == (
        "packagist", "monolog/monolog", "3.0.0",
    )


def test_parse_purl_npm_scope_percent_encoded():
    assert parse_purl("pkg:npm/%40babel/core@7.24.0") == ("npm", "@babel/core", "7.24.0")


def test_parse_purl_maven_uses_group_colon_artifact():
    assert parse_purl("pkg:maven/org.apache.commons/commons-lang3@3.14.0") == (
        "maven", "org.apache.commons:commons-lang3", "3.14.0",
    )


def test_parse_purl_go_module_path():
    assert parse_purl("pkg:golang/github.com/spf13/cobra@v1.8.0") == (
        "go", "github.com/spf13/cobra", "v1.8.0",
    )


def test_parse_purl_without_version():
    assert parse_purl("pkg:npm/express") == ("npm", "express", None)
    assert parse_purl("pkg:npm/%40scope/name") == ("npm", "@scope/name", None)


def test_parse_purl_drops_qualifiers_and_subpath():
    assert parse_purl("pkg:npm/express@4.17.1?arch=x86#sub/path") == (
        "npm", "express", "4.17.1",
    )


def test_parse_purl_rejects_actions_and_garbage():
    assert parse_purl("pkg:githubactions/actions/checkout@4") is None
    assert parse_purl("pkg:github/owner/repo@abc123") is None
    assert parse_purl("not-a-purl") is None
    assert parse_purl("pkg:npm") is None


# ---------------------------------------------------------------------------
# parse_github_sbom
# ---------------------------------------------------------------------------


def _sbom_payload() -> dict:
    return {
        "sbom": {
            "SPDXID": "SPDXRef-DOCUMENT",
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relatedSpdxElement": "SPDXRef-repo",
                    "relationshipType": "DESCRIBES",
                },
            ],
            "packages": [
                {  # the repo itself: excluded via the DESCRIBES relationship
                    "SPDXID": "SPDXRef-repo",
                    "name": "com.github.owner/repo",
                    "externalRefs": [
                        {"referenceType": "purl", "referenceLocator": "pkg:github/owner/repo@abc"}
                    ],
                },
                {
                    "SPDXID": "SPDXRef-npm-express",
                    "name": "express",
                    "versionInfo": "4.17.1",
                    "externalRefs": [
                        {"referenceType": "purl", "referenceLocator": "pkg:npm/express@4.17.1"}
                    ],
                },
                {  # older export style: prefixed name, no purl
                    "SPDXID": "SPDXRef-pip-requests",
                    "name": "pip:requests",
                    "versionInfo": "2.31.0",
                },
                {  # CI-only dependency: excluded
                    "SPDXID": "SPDXRef-action",
                    "name": "actions/checkout",
                    "externalRefs": [
                        {
                            "referenceType": "purl",
                            "referenceLocator": "pkg:githubactions/actions/checkout@4",
                        }
                    ],
                },
                {  # duplicate of express: deduplicated
                    "SPDXID": "SPDXRef-npm-express-dup",
                    "name": "express",
                    "externalRefs": [
                        {"referenceType": "purl", "referenceLocator": "pkg:npm/express@4.17.1"}
                    ],
                },
            ],
        }
    }


def test_parse_github_sbom_extracts_deps_and_skips_root_and_actions():
    assert parse_github_sbom(_sbom_payload()) == [
        ("npm", "express", "4.17.1"),
        ("pypi", "requests", "2.31.0"),
    ]


def test_parse_github_sbom_tolerates_malformed_payloads():
    assert parse_github_sbom(None) == []
    assert parse_github_sbom({}) == []
    assert parse_github_sbom({"sbom": "nope"}) == []
    assert parse_github_sbom({"sbom": {"packages": [None, {}, {"name": "??"}]}}) == []


# ---------------------------------------------------------------------------
# resolve_dependencies (direct/indirect classification)
# ---------------------------------------------------------------------------


def test_resolve_dependencies_classifies_and_sorts_direct_first():
    entries = [
        ("npm", "lodash", "4.17.21"),
        ("npm", "express", "4.17.1"),
        ("pypi", "typing_extensions", "4.9.0"),
    ]
    declared = [_dep("npm", "express"), _dep("pypi", "Typing.Extensions")]
    resolved = resolve_dependencies(entries, declared)
    assert [(d.name, d.direct) for d in resolved] == [
        ("express", True),
        ("typing_extensions", True),  # PEP 503 normalization matches the declared name
        ("lodash", False),
    ]


def test_resolve_dependencies_same_name_other_ecosystem_is_indirect():
    resolved = resolve_dependencies([("pypi", "express", None)], [_dep("npm", "express")])
    assert resolved[0].direct is False


# ---------------------------------------------------------------------------
# collect_all_dependencies (network, best-effort)
# ---------------------------------------------------------------------------


def test_collect_success():
    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo/dependency-graph/sbom"
        return httpx.Response(200, json=_sbom_payload())

    warnings: list[str] = []
    with _client(responder) as gh:
        result = collect_all_dependencies(
            gh, "owner", "repo", [_dep("npm", "express")], warnings
        )
    assert result.collected is True
    assert result.source == "github-sbom"
    assert result.error is None
    assert (result.total_count, result.direct_count, result.indirect_count) == (2, 1, 1)
    assert result.truncated is False
    assert [d.name for d in result.packages] == ["express", "requests"]
    assert warnings == []


def test_collect_404_reports_error_but_does_not_raise():
    warnings: list[str] = []
    with _client(lambda request: httpx.Response(404)) as gh:
        result = collect_all_dependencies(gh, "owner", "repo", [], warnings)
    assert result.collected is False
    assert result.packages == []
    assert "404" in result.error
    assert warnings == [result.error]


def test_collect_network_error_reports_error_but_does_not_raise():
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    warnings: list[str] = []
    with _client(responder) as gh:
        result = collect_all_dependencies(gh, "owner", "repo", [], warnings)
    assert result.collected is False
    assert "failed" in result.error
    assert warnings == [result.error]


def test_collect_invalid_payload_reports_error():
    warnings: list[str] = []
    with _client(lambda request: httpx.Response(200, json={"unexpected": True})) as gh:
        result = collect_all_dependencies(gh, "owner", "repo", [], warnings)
    assert result.collected is False
    assert "valid SPDX" in result.error
    assert warnings == [result.error]


def test_collect_skips_when_time_budget_exhausted():
    calls: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_sbom_payload())

    warnings: list[str] = []
    with _client(responder) as gh:
        result = collect_all_dependencies(
            gh, "owner", "repo", [], warnings, budget_seconds=0
        )
    assert calls == []  # no request once the budget is gone
    assert result.collected is False
    assert "time budget" in result.error
    assert warnings == [result.error]


def test_collect_truncates_giant_graphs(monkeypatch):
    monkeypatch.setattr(sbom, "MAX_PACKAGES_IN_REPORT", 2)
    payload = {
        "sbom": {
            "packages": [
                {
                    "SPDXID": f"SPDXRef-{i}",
                    "name": f"pkg{i}",
                    "externalRefs": [
                        {"referenceType": "purl", "referenceLocator": f"pkg:npm/pkg{i}@1.0.{i}"}
                    ],
                }
                for i in range(5)
            ],
        }
    }
    warnings: list[str] = []
    with _client(lambda request: httpx.Response(200, json=payload)) as gh:
        result = collect_all_dependencies(gh, "owner", "repo", [_dep("npm", "pkg3")], warnings)
    assert result.collected is True
    assert result.truncated is True
    assert (result.total_count, result.direct_count, result.indirect_count) == (5, 1, 4)
    assert len(result.packages) == 2
    assert result.packages[0].name == "pkg3"  # direct entries survive truncation first
    assert any("truncated" in w for w in warnings)
