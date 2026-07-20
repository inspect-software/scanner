"""Runtime dependency closure from deps.dev — pure parsing plus the
best-effort network path, exercised through an httpx mock transport."""

from __future__ import annotations

import httpx
import pytest

from scanner.models import EcosystemPackage
from scanner.runtime_deps import (
    collect_runtime_closure,
    depsdev_system,
    parse_dependency_nodes,
    primary_package,
)


def pkg(name="flask", ecosystem="pypi", version="3.1.3"):
    return EcosystemPackage(
        ecosystem=ecosystem,
        name=name,
        registry_url=f"https://example.invalid/{name}",
        latest_version=version,
    )


def node(name, version, relation):
    return {"versionKey": {"name": name, "version": version}, "relation": relation}


@pytest.mark.parametrize(
    "ours,expected",
    [("npm", "npm"), ("pypi", "pypi"), ("crates", "cargo"), ("maven", "maven")],
)
def test_ecosystems_map_to_depsdev_system_names(ours, expected):
    assert depsdev_system(ours) == expected


@pytest.mark.parametrize("ecosystem", ["go", "nuget", "rubygems", "packagist", "hex", "cocoapods"])
def test_ecosystems_without_graph_resolution_are_not_queried(ecosystem):
    """deps.dev indexes these, but its :dependencies endpoint 404s for them.
    Mapping them would spend a request and warn about a limitation that is not
    the repository's; they fall back to the repository graph silently."""
    assert depsdev_system(ecosystem) is None


def test_the_queried_package_itself_is_not_a_dependency():
    payload = {"nodes": [node("flask", "3.1.3", "SELF"), node("click", "8.4.2", "DIRECT")]}
    resolved = parse_dependency_nodes(payload, "pypi")
    assert [d.name for d in resolved] == ["click"]


def test_direct_and_transitive_relations_are_distinguished():
    payload = {
        "nodes": [
            node("flask", "3.1.3", "SELF"),
            node("werkzeug", "3.1.8", "DIRECT"),
            node("markupsafe", "3.0.3", "INDIRECT"),
        ]
    }
    resolved = parse_dependency_nodes(payload, "pypi")
    assert {d.name: d.direct for d in resolved} == {"werkzeug": True, "markupsafe": False}


def test_duplicate_nodes_are_collapsed():
    payload = {"nodes": [node("click", "8.4.2", "DIRECT"), node("Click", "8.4.2", "INDIRECT")]}
    assert len(parse_dependency_nodes(payload, "pypi")) == 1


def test_malformed_payloads_yield_nothing_rather_than_raising():
    assert parse_dependency_nodes({}, "pypi") == []
    assert parse_dependency_nodes({"nodes": "not-a-list"}, "pypi") == []
    assert parse_dependency_nodes({"nodes": [{"relation": "DIRECT"}]}, "pypi") == []


def test_primary_package_skips_entries_without_a_version():
    packages = [pkg("a", version=None), pkg("b", version="1.0.0")]
    assert primary_package(packages).name == "b"


def test_the_package_named_like_the_repository_wins():
    """Taking the first resolvable entry assessed `examples` for tokio-rs/tokio
    and `actioncable` for rails/rails — an incidental sibling, presented as if
    it were the project itself."""
    packages = [pkg("examples", ecosystem="crates"), pkg("tokio", ecosystem="crates")]
    assert primary_package(packages, "tokio").name == "tokio"


def test_scoped_names_still_match_the_repository():
    assert primary_package(
        [pkg("@rails/actioncable", ecosystem="npm")], "actioncable"
    ).name == "@rails/actioncable"


def test_without_a_name_match_the_first_resolvable_package_is_used():
    packages = [pkg("alpha", ecosystem="npm"), pkg("beta", ecosystem="npm")]
    assert primary_package(packages, "something-else").name == "alpha"


def test_primary_package_is_none_when_nothing_is_publishable():
    assert primary_package([]) is None
    assert primary_package([pkg("a", ecosystem="rubygems")]) is None


def test_collect_runtime_closure_happy_path():
    def handler(request):
        assert "/systems/pypi/packages/flask/versions/3.1.3" in str(request.url)
        return httpx.Response(
            200, json={"nodes": [node("flask", "3.1.3", "SELF"), node("click", "8.4.2", "DIRECT")]}
        )

    warnings: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        resolved = collect_runtime_closure(pkg(), warnings, client=client)

    assert [d.name for d in resolved] == ["click"]
    assert warnings == []


def test_unindexed_package_falls_back_with_a_warning():
    def handler(request):
        return httpx.Response(404, json={})

    warnings: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert collect_runtime_closure(pkg(), warnings, client=client) is None
    assert warnings and "repository dependency graph instead" in warnings[0]


def test_transport_failure_falls_back_and_never_raises():
    def handler(request):
        raise httpx.ConnectError("deps.dev unreachable")

    warnings: list[str] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert collect_runtime_closure(pkg(), warnings, client=client) is None
    assert warnings and "deps.dev unreachable" in warnings[0]


def test_a_package_with_no_dependencies_falls_back_rather_than_reporting_zero():
    """An empty closure is indistinguishable from an unresolved one here, and
    claiming "zero dependencies, all clean" from a thin payload would be a
    stronger statement than the data supports."""

    def handler(request):
        return httpx.Response(200, json={"nodes": [node("flask", "3.1.3", "SELF")]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert collect_runtime_closure(pkg(), [], client=client) is None
