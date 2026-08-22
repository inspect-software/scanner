"""Interface checks judge only software that could meaningfully lack them.

A Rust database driver was told it was missing an OpenAPI schema and an MCP
server, and its maintainer replied "I have no clue how any of those is
applicable to this project" (scylladb/scylla-rust-driver#1852). He was right:
an `examples/` directory — which nearly every library ships — was enough to
pull a project into being judged on interfaces its kind of software has no
reason to expose.

The guarantee these tests pin, beyond the fix itself: **no repository enters
the metric that was not already in it, and no outcome moves downward.** The
metric-level gate is unchanged, so the only effect of applicability is to turn
`missed` into `excluded`.
"""

from __future__ import annotations

import pytest

from scanner.metrics import metric_ai_interfaces
from scanner.models import (
    AIReadinessSignals,
    ArtifactSignals,
    EcosystemData,
    EcosystemPackage,
    RepoData,
    RepoInfo,
)


def _data(*, schema=None, mcp=False, examples=None, library=False, service=False, cli=False):
    """A repository whose classification is driven by real evidence.

    Labels are not set directly — ``classify`` derives them — so these tests
    exercise the same path production does.
    """
    packages = []
    structure = []
    if library:
        packages.append(EcosystemPackage(
            ecosystem="crates", name="scylla", registry_url="x",
            exists=True, matches_repo=True, declared_type="library",
        ))
    if service:
        # Real evidence, not a hand-set label: a k8s manifest plus a compose
        # file is what classify() reads as a deployable service.
        structure = ["tree.k8s", "tree.compose", "tree.dockerfile"]
    if cli:
        structure = ["tree.go_main"]
    return RepoData(
        repo=RepoInfo(primary_language="Go" if (service or cli) else "Rust"),
        ecosystem=EcosystemData(packages=packages),
        artifacts=ArtifactSignals(collected=True, structure=structure),
        ai_readiness=AIReadinessSignals(
            api_schema_files=schema or [],
            has_mcp_signal=mcp,
            example_dirs=examples or [],
        ),
    )


def _by_name(metric, name):
    return next(c for c in metric.components if c.name.startswith(name))


# --- the gate is untouched -------------------------------------------------


def test_a_repository_with_no_interfaces_is_still_excluded_entirely():
    """The protection that keeps this metric off repositories it has nothing
    to say about. Removing it would turn today's exclusion into a zero for
    every library without an examples directory."""
    assert metric_ai_interfaces(_data(library=True)) is None


def test_examples_alone_still_admits_the_repository():
    assert metric_ai_interfaces(_data(library=True, examples=["examples"])) is not None


# --- applicability ---------------------------------------------------------


def test_a_library_with_examples_is_not_judged_on_schema_or_mcp():
    """The reported case: scylla and darling both scored 40 for shipping
    examples and nothing else."""
    metric = metric_ai_interfaces(_data(library=True, examples=["examples"]))

    assert _by_name(metric, "API schema").status == "excluded"
    assert _by_name(metric, "MCP server").status == "excluded"
    assert _by_name(metric, "Runnable examples").status == "met"
    assert metric.value == 100


def test_a_network_service_is_judged_on_both():
    metric = metric_ai_interfaces(_data(service=True, examples=["examples"]))

    assert _by_name(metric, "API schema").status == "missed"
    assert _by_name(metric, "MCP server").status == "missed"
    assert metric.value < 100


def test_a_bare_application_is_not_asked_for_an_mcp_server():
    """Telling ripgrep it lacks an AI protocol server is the same nonsense as
    telling a database driver, one step over."""
    metric = metric_ai_interfaces(_data(cli=True, examples=["examples"]))
    assert _by_name(metric, "MCP server").status == "excluded"


# --- presence always counts ------------------------------------------------


def test_a_library_shipping_a_schema_is_credited_for_it():
    """Classification decides whether absence is a finding, never whether
    evidence is real."""
    metric = metric_ai_interfaces(
        _data(library=True, schema=["openapi.yaml"], examples=["examples"])
    )
    assert _by_name(metric, "API schema").status == "met"


def test_a_library_shipping_an_mcp_server_is_credited_for_it():
    metric = metric_ai_interfaces(_data(library=True, mcp=True, examples=["examples"]))
    assert _by_name(metric, "MCP server").status == "met"


def test_mcp_applicability_never_depends_only_on_the_mcp_signal():
    """The circularity that would make the check unfailable: if the only way
    to be judged on MCP were to already have MCP, the component could never
    fail and would measure nothing."""
    without = metric_ai_interfaces(_data(service=True, examples=["examples"]))
    assert _by_name(without, "MCP server").status == "missed"


# --- no downward movement --------------------------------------------------


@pytest.mark.parametrize("kwargs", [
    dict(library=True, examples=["examples"]),
    dict(library=True, schema=["openapi.yaml"]),
    dict(library=True, mcp=True),
    dict(cli=True, examples=["examples"]),
    dict(service=True, examples=["examples"]),
])
def test_excluding_a_check_never_lowers_the_metric(kwargs):
    """Renormalizing over the applicable checks can only raise the value: the
    excluded ones were contributing zero out of a nonzero weight."""
    metric = metric_ai_interfaces(_data(**kwargs))
    scored = [c for c in metric.components if c.status != "excluded"]
    earned = sum(c.points for c in scored)
    possible = sum(c.max_points for c in scored)
    all_possible = sum(c.max_points for c in metric.components)
    assert possible <= all_possible
    assert metric.value >= max(1, round(100 * earned / all_possible))


def test_the_inputs_name_which_interfaces_were_expected():
    metric = metric_ai_interfaces(_data(service=True, examples=["examples"]))
    assert metric.inputs["interfaces_expected_of"] == ["network-service"]

    library = metric_ai_interfaces(_data(library=True, examples=["examples"]))
    assert library.inputs["interfaces_expected_of"] == []
