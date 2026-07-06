"""Metric computation: turn raw RepoData into standardized 1..100 scores.

Design rules (see docs/metrics.md for the full methodology):

- Every metric is an integer in 1..100 — higher is better.
- Values map to standardized bands: critical / at_risk / moderate / good /
  excellent (thresholds in ``BAND_THRESHOLDS``).
- Each metric is a weighted sum of named components. Every component is
  reported in the output with its earned/max points and a status: met,
  partial, missed, or excluded. Excluded components (no data, or not
  applicable) are removed from scoring and the remaining weights are
  renormalized — missing data is never counted as zero.
- If every component of a metric is excluded, the metric itself is None.
- Formulas are deterministic and versioned via ``METRICS_VERSION``.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from .models import Band, Metric, MetricComponent, Metrics, OrgData, OrgMetrics, RepoData

METRICS_VERSION = "0.3.0"

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

# Weights of each organization metric in the org overall score.
ORG_OVERALL_WEIGHTS: dict[str, float] = {
    "portfolio_activity": 0.45,
    "community_reach": 0.30,
    "profile_completeness": 0.25,
}


def band_for(value: int) -> Band:
    for threshold, band in BAND_THRESHOLDS:
        if value >= threshold:
            return band
    return "critical"


def _comp(
    name: str,
    max_points: float,
    earned: Optional[float],
    detail: Optional[str] = None,
) -> MetricComponent:
    """Build a component; ``earned=None`` marks it excluded (no data / N.A.)."""
    if earned is None:
        return MetricComponent(
            name=name,
            points=0.0,
            max_points=max_points,
            status="excluded",
            detail=detail or "no data",
        )
    earned = max(0.0, min(earned, max_points))
    if earned >= max_points - 1e-9:
        status = "met"
    elif earned <= 1e-9:
        status = "missed"
    else:
        status = "partial"
    return MetricComponent(
        name=name, points=round(earned, 1), max_points=max_points, status=status, detail=detail
    )


def _check(name: str, condition: bool, weight: float, detail: Optional[str] = None) -> MetricComponent:
    """Boolean checklist component: full points or none."""
    return _comp(name, weight, weight if condition else 0.0, detail)


def _metric(
    key: str,
    name: str,
    components: list[MetricComponent],
    inputs: dict[str, Any],
) -> Optional[Metric]:
    scored = [c for c in components if c.status != "excluded"]
    possible = sum(c.max_points for c in scored)
    if possible <= 0:
        return None
    earned = sum(c.points for c in scored)
    value = max(1, min(100, round(100 * earned / possible)))
    excluded = [c.name for c in components if c.status == "excluded"]
    return Metric(
        key=key,
        name=name,
        value=value,
        band=band_for(value),
        components=components,
        inputs=inputs,
        note=f"Excluded from scoring (no data or not applicable): {', '.join(excluded)}. "
        "Remaining weights renormalized."
        if excluded
        else None,
    )


# ---------------------------------------------------------------------------
# Individual metrics
# ---------------------------------------------------------------------------


def metric_activity(data: RepoData) -> Optional[Metric]:
    """Is the project actively developed? (recency, cadence, volume, releases)"""
    a = data.activity

    if a.days_since_last_push is not None:
        d = a.days_since_last_push
        pts = 35.0 if d <= 7 else 28.0 if d <= 30 else 18.0 if d <= 90 else 10.0 if d <= 180 else 4.0 if d <= 365 else 0.0
        recency = _comp("Push recency", 35, pts, f"last push {d} days ago")
    else:
        recency = _comp("Push recency", 35, None)

    if a.active_weeks_last_year is not None:
        w = min(a.active_weeks_last_year, 52)
        cadence = _comp("Commit cadence", 35, 35.0 * w / 52, f"{w}/52 weeks with commits")
    else:
        cadence = _comp("Commit cadence", 35, None)

    if a.commits_last_year is not None:
        n = a.commits_last_year
        volume = _comp(
            "Commit volume", 15, min(15.0, math.log10(n + 1) * 7.5), f"{n} commits in the last year"
        )
    else:
        volume = _comp("Commit volume", 15, None)

    if a.releases_count is None:
        releases = _comp("Release practice", 15, None)
    elif not a.releases_count:
        releases = _comp("Release practice", 15, 0.0, "no releases published")
    elif a.mean_days_between_releases is not None:
        gap = a.mean_days_between_releases
        pts = 15.0 if gap <= 45 else 10.0 if gap <= 120 else 6.0
        releases = _comp("Release practice", 15, pts, f"a release every ~{gap:g} days")
    else:
        releases = _comp("Release practice", 15, 6.0, f"{a.releases_count} releases, cadence unknown")

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
    bus = _comp("Bus factor", 60, bf_pts, f"{bf} contributor(s) cover half of all commits")

    if m.top_contributor_share is not None:
        share = m.top_contributor_share
        distribution = _comp(
            "Commit distribution", 25, (1.0 - share) * 25.0,
            f"top contributor authored {share:.0%} of commits",
        )
    else:
        distribution = _comp("Commit distribution", 25, None)

    if m.contributors_sampled is not None:
        breadth = _comp(
            "Contributor breadth", 15, min(15.0, m.contributors_sampled * 1.5),
            f"{m.contributors_sampled} contributors",
        )
    else:
        breadth = _comp("Contributor breadth", 15, None)

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

    if issues.closed_ratio is not None:
        issue_component = _comp(
            "Issue resolution", 55, issues.closed_ratio * 55.0,
            f"{issues.closed_ratio:.0%} of issues closed",
        )
    else:
        issue_component = _comp("Issue resolution", 55, None, "no issues or no data")

    pr_component = _comp("PR acceptance", 45, None, "no decided pull requests or no data")
    if issues.merged_prs is not None and issues.closed_unmerged_prs is not None:
        decided = issues.merged_prs + issues.closed_unmerged_prs
        if decided > 0:
            pr_component = _comp(
                "PR acceptance", 45, issues.merged_prs / decided * 45.0,
                f"{issues.merged_prs}/{decided} decided PRs merged",
            )

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
    checklist = [
        _check("README", c.has_readme, 25),
        _check("License", c.has_license, 20),
        _check("CONTRIBUTING guide", c.has_contributing, 15),
        _check("Code of conduct", c.has_code_of_conduct, 10),
        _check("Issue template", c.has_issue_template, 10),
        _check("Docs directory", q.has_docs_dir, 10),
        _check("PR template", c.has_pull_request_template, 5),
        _check("Repo description", c.has_description, 5),
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
    checklist = [
        _check(
            "CI workflows", q.has_ci, 30,
            f"{len(q.ci_workflows)} workflow(s)" if q.has_ci else None,
        ),
        _check("Tests present", q.has_tests, 30),
        _check(
            "Linter config", q.has_linter_config, 15,
            ", ".join(q.linter_configs) if q.linter_configs else None,
        ),
        _check("Pre-commit hooks", q.has_precommit_config, 10),
        _check("Docs directory", q.has_docs_dir, 10),
        _check(".editorconfig", q.has_editorconfig, 5),
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
    components = [
        _check("Security policy (SECURITY.md)", s.has_security_policy, 30),
        _check("Dependabot config", s.has_dependabot_config, 25),
        # Lockfile pinning only applies when the repo declares dependencies.
        _check(
            "Dependency lockfiles", bool(s.lockfiles), 25,
            ", ".join(s.lockfiles) if s.lockfiles else None,
        )
        if data.dependencies.manifests
        else _comp("Dependency lockfiles", 25, None, "no dependency manifests — not applicable"),
        _check("CodeQL workflow", s.has_codeql_workflow, 20),
    ]
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


def metric_overall(
    computed: dict[str, Optional[Metric]],
    weights: dict[str, float] = OVERALL_WEIGHTS,
    name: str = "Overall health",
) -> Optional[Metric]:
    """Weighted mean of available metrics, weights renormalized."""
    available = {k: m for k, m in computed.items() if m is not None}
    if not available:
        return None
    total_weight = sum(weights[k] for k in available)
    value = round(sum(m.value * weights[k] for k, m in available.items()) / total_weight)
    value = max(1, min(100, value))
    missing = sorted(set(weights) - set(available))
    return Metric(
        key="overall",
        name=name,
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


# ---------------------------------------------------------------------------
# Organization metrics
# ---------------------------------------------------------------------------


def metric_org_profile(data: OrgData) -> Optional[Metric]:
    """Is the organization profile complete and accountable?"""
    info = data.info
    checklist = [
        _check("Verified domain", info.is_verified, 25),
        _check("Description", bool(info.description), 20),
        _check("Homepage", bool(info.blog), 15, info.blog or None),
        _check("Display name", bool(info.name), 10),
        _check("Location", bool(info.location), 10),
        _check("Contact email", bool(info.email), 10),
        _check("Social profile", bool(info.twitter_username), 10),
    ]
    return _metric(
        "profile_completeness",
        "Profile completeness",
        checklist,
        {
            "is_verified": info.is_verified,
            "description": bool(info.description),
            "blog": info.blog,
            "name": info.name,
            "location": info.location,
            "email": bool(info.email),
            "twitter_username": info.twitter_username,
        },
    )


def metric_org_portfolio(data: OrgData) -> Optional[Metric]:
    """Is the organization's repository portfolio actively maintained?"""
    p = data.portfolio
    info = data.info

    if p.repos_sampled > 0 and p.repos_pushed_90d is not None:
        ratio = p.repos_pushed_90d / p.repos_sampled
        recent = _comp(
            "Recently active repos", 50, ratio * 50.0,
            f"{p.repos_pushed_90d}/{p.repos_sampled} sampled repos pushed in the last 90 days",
        )
    else:
        recent = _comp("Recently active repos", 50, None)

    if p.repos_sampled > 0 and p.repos_pushed_365d is not None:
        ratio = p.repos_pushed_365d / p.repos_sampled
        yearly = _comp(
            "Yearly active repos", 25, ratio * 25.0,
            f"{p.repos_pushed_365d}/{p.repos_sampled} sampled repos pushed in the last year",
        )
    else:
        yearly = _comp("Yearly active repos", 25, None)

    size = _comp(
        "Portfolio size", 15, min(15.0, math.log10(info.public_repos + 1) * 7.5),
        f"{info.public_repos} public repositories",
    )

    if p.repos_sampled > 0:
        ratio = p.original_repos_sampled / p.repos_sampled
        original = _comp(
            "Original work", 10, ratio * 10.0,
            f"{p.original_repos_sampled}/{p.repos_sampled} sampled repos are not forks",
        )
    else:
        original = _comp("Original work", 10, None)

    return _metric(
        "portfolio_activity",
        "Portfolio activity",
        [recent, yearly, size, original],
        {
            "public_repos": info.public_repos,
            "repos_sampled": p.repos_sampled,
            "repos_pushed_90d": p.repos_pushed_90d,
            "repos_pushed_365d": p.repos_pushed_365d,
            "forks_sampled": p.forks_sampled,
        },
    )


def metric_org_reach(data: OrgData) -> Optional[Metric]:
    """Does the organization have community traction?"""
    info = data.info
    p = data.portfolio
    followers = _comp(
        "Followers", 50, min(50.0, math.log10(info.followers + 1) * 50 / 3),
        f"{info.followers} followers",
    )
    stars = _comp(
        "Stars across repositories", 50,
        min(50.0, math.log10(p.total_stars_sampled + 1) * 12.5),
        f"{p.total_stars_sampled} stars across {p.repos_sampled} sampled repos",
    )
    return _metric(
        "community_reach",
        "Community reach",
        [followers, stars],
        {
            "followers": info.followers,
            "total_stars_sampled": p.total_stars_sampled,
            "repos_sampled": p.repos_sampled,
        },
    )


def compute_org_metrics(data: OrgData) -> OrgMetrics:
    computed = {
        "profile_completeness": metric_org_profile(data),
        "portfolio_activity": metric_org_portfolio(data),
        "community_reach": metric_org_reach(data),
    }
    return OrgMetrics(
        metrics_version=METRICS_VERSION,
        overall=metric_overall(computed, ORG_OVERALL_WEIGHTS, "Overall organization health"),
        **computed,
    )
