"""Component weights sum to 100, as the methodology's first scoring rule says.

Nothing crashes when they do not: a metric's value is
``round(100 × Σpoints / Σmax_points)``, so the sum normalizes away. That is
precisely why the invariant needs a test — a mis-set weight silently changes
every component's real share of its metric while every number still looks
plausible. This suite exists because that happened: a new component was added
to ``ai_agent_context`` without reducing the one it took weight from, leaving
the metric summing to 115 and the new component worth 35% where 40% was
intended.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scanner import metrics as metrics_module
from scanner.models import (
    Activity,
    AIReadinessSignals,
    CommitRecord,
    CommunityHealth,
    IssueMetrics,
    Maintainership,
    Popularity,
    QualitySignals,
    RepoData,
    RepoInfo,
)

# community_health is the documented exception: its checklist weights
# (22.5 + 22.5 + 18 + 13.5 + 7.2 + 6.3) have summed to 90 since they were
# written, in metrics.md as well as in code. Left alone deliberately —
# rebalancing it would move every repository's score for no gain in accuracy,
# since the sum normalizes away.
KNOWN_SUM_EXCEPTIONS = {"community_health": 90.0}


def _populated_repo_data() -> RepoData:
    """A RepoData complete enough that no component is excluded for want of data."""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    commits = [
        CommitRecord(
            oid=f"{i:040d}",
            committed_at=base - timedelta(days=i),
            headline="fix(core): correct the thing (#123)",
        )
        for i in range(100)
    ]
    return RepoData(
        repo=RepoInfo(primary_language="Python"),
        activity=Activity(
            days_since_last_push=2, active_weeks_last_year=50, commits_last_year=400,
            recent_commits=commits, releases_count=5, days_since_latest_release=10,
        ),
        popularity=Popularity(stars=100, forks=10, watchers=5),
        maintainership=Maintainership(
            bus_factor=3, top_contributor_share=0.4, contributors_sampled=10,
            issues=IssueMetrics(closed_ratio=0.8, merged_prs=5, closed_unmerged_prs=1),
        ),
        community=CommunityHealth(has_readme=True, has_license=True),
        quality_signals=QualitySignals(has_ci=True, has_tests=True),
        ai_readiness=AIReadinessSignals(
            agent_instruction_files=["CLAUDE.md"], agent_instruction_max_bytes=5000
        ),
    )


def _repo_metrics():
    """Every repository metric that scores on the populated fixture."""
    data = _populated_repo_data()
    for name in sorted(dir(metrics_module)):
        if not name.startswith("metric_") or name.startswith("metric_org_"):
            continue
        metric = getattr(metrics_module, name)(data)
        if metric is not None:
            yield metric


def test_at_least_the_known_metrics_are_covered():
    # Guards the discovery above: a rename that silently stopped finding
    # metrics would make every assertion below vacuously pass.
    keys = {m.key for m in _repo_metrics()}
    assert {"development_activity", "ai_agent_context", "ai_verify_loop"} <= keys
    assert len(keys) >= 10


@pytest.mark.parametrize("metric", list(_repo_metrics()), ids=lambda m: m.key)
def test_component_weights_sum_to_one_hundred(metric):
    expected = KNOWN_SUM_EXCEPTIONS.get(metric.key, 100.0)
    total = sum(component.max_points for component in metric.components)
    assert total == pytest.approx(expected), (
        f"{metric.key} components sum to {total}, expected {expected}"
    )
