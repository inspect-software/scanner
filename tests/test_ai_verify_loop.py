"""Verify loop: bootstrap credit across ecosystems, and demonstrated practice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scanner.metrics import AGENT_COMMIT_SHARE_FULL_MARKS, metric_ai_verify_loop
from scanner.models import (
    Activity,
    AIReadinessSignals,
    CommitRecord,
    QualitySignals,
    RepoData,
    RepoInfo,
)


def _commits(count=100, agents=0):
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return [
        CommitRecord(
            oid=f"{i:040d}",
            committed_at=base - timedelta(days=i),
            headline="Do a thing",
            is_coding_agent=i < agents,
        )
        for i in range(count)
    ]


def _metric(ai=None, commits=None, language="Python"):
    data = RepoData(
        repo=RepoInfo(primary_language=language),
        ai_readiness=ai or AIReadinessSignals(),
        quality_signals=QualitySignals(has_tests=True),
        activity=Activity(recent_commits=_commits() if commits is None else commits),
    )
    metric = metric_ai_verify_loop(data)
    return metric, {c.name: c for c in metric.components}


# --- one-command bootstrap ---------------------------------------------------


def test_task_runner_earns_full_bootstrap_credit():
    _, components = _metric(AIReadinessSignals(bootstrap_files=["Makefile"]))
    bootstrap = components["One-command bootstrap"]
    assert bootstrap.points == bootstrap.max_points


def test_toolchain_convention_earns_most_of_the_credit():
    # rust-lang/regex and serde ship no Makefile and scored zero here, though
    # `cargo test` is the canonical verify loop for the whole ecosystem.
    _, components = _metric(AIReadinessSignals(toolchain_manifests=["Cargo.toml"]), language="Rust")
    bootstrap = components["One-command bootstrap"]
    assert 0 < bootstrap.points < bootstrap.max_points
    assert "toolchain convention" in bootstrap.detail


def test_a_task_runner_outranks_a_bare_toolchain():
    _, runner = _metric(AIReadinessSignals(bootstrap_files=["justfile"], toolchain_manifests=["go.mod"]))
    _, toolchain = _metric(AIReadinessSignals(toolchain_manifests=["go.mod"]))
    assert runner["One-command bootstrap"].points > toolchain["One-command bootstrap"].points


def test_neither_earns_nothing():
    _, components = _metric(AIReadinessSignals())
    assert components["One-command bootstrap"].points == 0


# --- demonstrated agent practice ---------------------------------------------


def test_agent_commits_demonstrate_the_loop():
    # gin-gonic/gin: 11 of its newest 100 commits credit Claude Code or Copilot.
    _, components = _metric(commits=_commits(agents=11))
    demonstrated = components["Demonstrated agent practice"]
    assert demonstrated.points == demonstrated.max_points
    assert "11 of the last 100 commits" in demonstrated.detail


def test_a_single_agent_commit_earns_partial_credit():
    # psf/requests: exactly one. Trying it once is not adopting the practice.
    _, components = _metric(commits=_commits(agents=1))
    demonstrated = components["Demonstrated agent practice"]
    assert 0 < demonstrated.points < demonstrated.max_points


def test_no_agent_commits_earns_nothing_but_says_so():
    _, components = _metric(commits=_commits(agents=0))
    demonstrated = components["Demonstrated agent practice"]
    assert demonstrated.points == 0
    assert "no agent-authored commits" in demonstrated.detail


def test_full_marks_threshold_matches_the_documented_share():
    agents = int(100 * AGENT_COMMIT_SHARE_FULL_MARKS)
    _, components = _metric(commits=_commits(agents=agents))
    assert components["Demonstrated agent practice"].points == 10


def test_missing_commit_sample_is_excluded_not_zero():
    # An unauthenticated scan collects no commits; it must not be marked down
    # for evidence it could not observe.
    metric, components = _metric(commits=[])
    assert components["Demonstrated agent practice"].status == "excluded"
    assert metric.inputs["agent_commit_share"] is None


def test_excluding_the_component_does_not_lower_the_metric():
    without_sample, _ = _metric(commits=[])
    with_no_agents, _ = _metric(commits=_commits(agents=0))
    assert without_sample.value > with_no_agents.value
