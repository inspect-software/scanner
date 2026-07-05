"""Metric computation: turn raw RepoData into standardized 1..100 scores.

Design rules (see docs/metrics.md for the full methodology):

- Every metric is an integer in 1..100 — higher is better.
- Values map to standardized bands: critical / at_risk / moderate / good /
  excellent (thresholds in ``BAND_THRESHOLDS``).
- Each metric is a weighted sum of components. When a component's underlying
  data is unavailable (None), the component is *excluded* and the remaining
  weights are renormalized — missing data is never counted as zero.
- If no component of a metric has data, the metric itself is None.
- Formulas are deterministic and versioned via ``METRICS_VERSION``.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from .models import Band, Metric, Metrics, RepoData

METRICS_VERSION = "0.1.0"

# Lower bound of each band, checked from the top down.
BAND_THRESHOLDS: list[tuple[int, Band]] = [
    (85, "excellent"),
    (70, "good"),
    (50, "moderate"),
    (30, "at_risk"),
    (1, "critical"),
]

# Weights of each metric in the overall score (renormalized over available).
OVERALL_WEIGHTS: dict[str, float] = {
    "activity": 0.20,
    "maintainer_resilience": 0.20,
    "security_posture": 0.15,
    "engineering_practices": 0.15,
    "responsiveness": 0.15,
    "community_health": 0.15,
}


def band_for(value: int) -> Band:
    for threshold, band in BAND_THRESHOLDS:
        if value >= threshold:
            return band
    return "critical"


# A component is (earned_points, max_points); None means "no data, exclude".
Component = Optional[tuple[float, float]]


def _score(components: list[Component]) -> Optional[tuple[int, bool]]:
    """Combine components into a 1..100 value.

    Returns (value, had_missing_components) or None when no component has data.
    Excluded (None) components are removed and the scale is renormalized.
    """
    available = [c for c in components if c is not None]
    possible = sum(maximum for _, maximum in available)
    if possible <= 0:
        return None
    earned = sum(earned for earned, _ in available)
    value = max(1, min(100, round(100 * earned / possible)))
    return value, len(available) < len(components)


def _metric(
    key: str,
    name: str,
    components: list[Component],
    inputs: dict[str, Any],
) -> Optional[Metric]:
    scored = _score(components)
    if scored is None:
        return None
    value, had_missing = scored
    return Metric(
        key=key,
        name=name,
        value=value,
        band=band_for(value),
        inputs=inputs,
        note="Some inputs unavailable; score computed from remaining components"
        if had_missing
        else None,
    )


# ---------------------------------------------------------------------------
# Individual metrics
# ---------------------------------------------------------------------------


def metric_activity(data: RepoData) -> Optional[Metric]:
    """Is the project actively developed? (recency, cadence, volume, releases)"""
    a = data.activity

    recency: Component = None
    if a.days_since_last_push is not None:
        d = a.days_since_last_push
        pts = 35.0 if d <= 7 else 28.0 if d <= 30 else 18.0 if d <= 90 else 10.0 if d <= 180 else 4.0 if d <= 365 else 0.0
        recency = (pts, 35.0)

    cadence: Component = None
    if a.active_weeks_last_year is not None:
        cadence = (35.0 * min(a.active_weeks_last_year, 52) / 52, 35.0)

    volume: Component = None
    if a.commits_last_year is not None:
        # log scale: 10 commits ≈ 7.5 pts, 100 ≈ 15 pts (cap)
        volume = (min(15.0, math.log10(a.commits_last_year + 1) * 7.5), 15.0)

    releases: Component = None
    if a.releases_count is not None:
        if not a.releases_count:
            releases = (0.0, 15.0)
        elif a.mean_days_between_releases is not None:
            gap = a.mean_days_between_releases
            pts = 15.0 if gap <= 45 else 10.0 if gap <= 120 else 6.0
            releases = (pts, 15.0)
        else:
            releases = (6.0, 15.0)

    return _metric(
        "activity",
        "Development activity",
        [recency, cadence, volume, releases],
        {
            "days_since_last_push": a.days_since_last_push,
            "active_weeks_last_year": a.active_weeks_last_year,
            "commits_last_year": a.commits_last_year,
            "releases_count": a.releases_count,
            "mean_days_between_releases": a.mean_days_between_releases,
        },
    )


def metric_maintainer_resilience(data: RepoData) -> Optional[Metric]:
    """Can the project survive losing its top maintainer? (bus factor)"""
    m = data.maintainership
    if m.bus_factor is None:
        return None

    bf = m.bus_factor
    bf_pts = {1: 10.0, 2: 28.0, 3: 40.0, 4: 48.0}.get(bf, min(60.0, 48.0 + (bf - 4) * 3.0))
    bus: Component = (bf_pts, 60.0)

    distribution: Component = None
    if m.top_contributor_share is not None:
        distribution = ((1.0 - m.top_contributor_share) * 25.0, 25.0)

    breadth: Component = None
    if m.contributors_sampled is not None:
        breadth = (min(15.0, m.contributors_sampled * 1.5), 15.0)

    return _metric(
        "maintainer_resilience",
        "Maintainer resilience (bus factor)",
        [bus, distribution, breadth],
        {
            "bus_factor": m.bus_factor,
            "top_contributor_share": m.top_contributor_share,
            "contributors_sampled": m.contributors_sampled,
        },
    )


def metric_responsiveness(data: RepoData) -> Optional[Metric]:
    """Are issues and pull requests actually being handled?"""
    issues = data.maintainership.issues

    issue_component: Component = None
    if issues.closed_ratio is not None:
        issue_component = (issues.closed_ratio * 55.0, 55.0)

    pr_component: Component = None
    if issues.merged_prs is not None and issues.closed_unmerged_prs is not None:
        decided = issues.merged_prs + issues.closed_unmerged_prs
        if decided > 0:
            pr_component = (issues.merged_prs / decided * 45.0, 45.0)

    return _metric(
        "responsiveness",
        "Issue & PR responsiveness",
        [issue_component, pr_component],
        {
            "open_issues": issues.open_issues,
            "closed_issues": issues.closed_issues,
            "issue_closed_ratio": issues.closed_ratio,
            "merged_prs": issues.merged_prs,
            "closed_unmerged_prs": issues.closed_unmerged_prs,
        },
    )


def metric_community_health(data: RepoData) -> Optional[Metric]:
    """Is the project set up to receive users and contributors?"""
    c = data.community
    q = data.quality_signals
    checklist: list[Component] = [
        (25.0 if c.has_readme else 0.0, 25.0),
        (20.0 if c.has_license else 0.0, 20.0),
        (15.0 if c.has_contributing else 0.0, 15.0),
        (10.0 if c.has_code_of_conduct else 0.0, 10.0),
        (10.0 if c.has_issue_template else 0.0, 10.0),
        (5.0 if c.has_pull_request_template else 0.0, 5.0),
        (5.0 if c.has_description else 0.0, 5.0),
        (10.0 if q.has_docs_dir else 0.0, 10.0),
    ]
    return _metric(
        "community_health",
        "Community health & documentation",
        checklist,
        {
            "has_readme": c.has_readme,
            "has_license": c.has_license,
            "has_contributing": c.has_contributing,
            "has_code_of_conduct": c.has_code_of_conduct,
            "has_issue_template": c.has_issue_template,
            "has_pull_request_template": c.has_pull_request_template,
            "has_description": c.has_description,
            "has_docs_dir": q.has_docs_dir,
        },
    )


def metric_engineering_practices(data: RepoData) -> Optional[Metric]:
    """Baseline engineering hygiene: CI, tests, linting."""
    q = data.quality_signals
    checklist: list[Component] = [
        (30.0 if q.has_ci else 0.0, 30.0),
        (30.0 if q.has_tests else 0.0, 30.0),
        (15.0 if q.has_linter_config else 0.0, 15.0),
        (10.0 if q.has_precommit_config else 0.0, 10.0),
        (10.0 if q.has_docs_dir else 0.0, 10.0),
        (5.0 if q.has_editorconfig else 0.0, 5.0),
    ]
    return _metric(
        "engineering_practices",
        "Engineering practices",
        checklist,
        {
            "has_ci": q.has_ci,
            "has_tests": q.has_tests,
            "has_linter_config": q.has_linter_config,
            "has_precommit_config": q.has_precommit_config,
            "has_docs_dir": q.has_docs_dir,
            "has_editorconfig": q.has_editorconfig,
        },
    )


def metric_security_posture(data: RepoData) -> Optional[Metric]:
    """Visible security hygiene: policy, automated updates, scanning, pinning."""
    s = data.security_signals
    components: list[Component] = [
        (30.0 if s.has_security_policy else 0.0, 30.0),
        (25.0 if s.has_dependabot_config else 0.0, 25.0),
        (20.0 if s.has_codeql_workflow else 0.0, 20.0),
    ]
    # Lockfile pinning only applies when the repo declares dependencies.
    if data.dependencies.manifests:
        components.append((25.0 if s.lockfiles else 0.0, 25.0))
    return _metric(
        "security_posture",
        "Security posture",
        components,
        {
            "has_security_policy": s.has_security_policy,
            "has_dependabot_config": s.has_dependabot_config,
            "has_codeql_workflow": s.has_codeql_workflow,
            "lockfiles": s.lockfiles,
            "manifests": data.dependencies.manifests,
        },
    )


def metric_overall(computed: dict[str, Optional[Metric]]) -> Optional[Metric]:
    """Weighted mean of available metrics, weights renormalized."""
    available = {k: m for k, m in computed.items() if m is not None}
    if not available:
        return None
    total_weight = sum(OVERALL_WEIGHTS[k] for k in available)
    value = round(
        sum(m.value * OVERALL_WEIGHTS[k] for k, m in available.items()) / total_weight
    )
    value = max(1, min(100, value))
    missing = sorted(set(OVERALL_WEIGHTS) - set(available))
    return Metric(
        key="overall",
        name="Overall health",
        value=value,
        band=band_for(value),
        inputs={k: m.value for k, m in available.items()},
        note=f"Missing metrics excluded and weights renormalized: {', '.join(missing)}"
        if missing
        else None,
    )


def compute_metrics(data: RepoData) -> Metrics:
    computed = {
        "activity": metric_activity(data),
        "maintainer_resilience": metric_maintainer_resilience(data),
        "responsiveness": metric_responsiveness(data),
        "community_health": metric_community_health(data),
        "engineering_practices": metric_engineering_practices(data),
        "security_posture": metric_security_posture(data),
    }
    return Metrics(
        metrics_version=METRICS_VERSION,
        overall=metric_overall(computed),
        **computed,
    )
