"""Metric computation: turn raw RepoData into standardized 1..100 scores.

Design rules (see docs/metrics.md for the full methodology):

- Every metric is an integer in 1..100 — higher is better.
- Values map to standardized bands: critical / at_risk / moderate / good /
  excellent (thresholds in ``BAND_THRESHOLDS``).
- Each metric is a weighted sum of named components. Every component is
  reported with its earned/max points and a status: met, partial, missed, or
  excluded. Excluded components (no data, or not applicable) are removed and
  the remaining weights renormalized — missing data is never counted as zero.
- Metrics are grouped into weighted **categories**; each category has a
  rolled-up score (weighted mean of its available metrics). The overall score
  is the weighted mean of the available categories.
- If every component of a metric is excluded, the metric is None; a category
  with no scorable metric is dropped.
- Formulas are deterministic and versioned via ``METRICS_VERSION``.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Optional

from .models import (
    Band,
    Metric,
    MetricCategory,
    MetricComponent,
    Metrics,
    OrgData,
    OrgMetrics,
    RepoData,
)

METRICS_VERSION = "0.4.0"

# Lower bound of each band, checked from the top down.
BAND_THRESHOLDS: list[tuple[int, Band]] = [
    (85, "excellent"),
    (70, "good"),
    (50, "moderate"),
    (30, "at_risk"),
    (1, "critical"),
]


def band_for(value: int) -> Band:
    for threshold, band in BAND_THRESHOLDS:
        if value >= threshold:
            return band
    return "critical"


def _log_points(value: int, max_points: float, saturating_value: float) -> float:
    """Log-scaled points: 0 at value 0, ``max_points`` at ``saturating_value``."""
    if value <= 0:
        return 0.0
    scale = max_points / math.log10(saturating_value + 1)
    return min(max_points, math.log10(value + 1) * scale)


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
# Repository metrics
# ---------------------------------------------------------------------------


def metric_development_activity(data: RepoData) -> Optional[Metric]:
    """Is code actively being written? (push recency, cadence, volume)"""
    a = data.activity

    if a.days_since_last_push is not None:
        d = a.days_since_last_push
        pts = 40.0 if d <= 7 else 32.0 if d <= 30 else 20.0 if d <= 90 else 11.0 if d <= 180 else 4.0 if d <= 365 else 0.0
        recency = _comp("Push recency", 40, pts, f"last push {d} days ago")
    else:
        recency = _comp("Push recency", 40, None)

    if a.active_weeks_last_year is not None:
        w = min(a.active_weeks_last_year, 52)
        cadence = _comp("Commit cadence", 40, 40.0 * w / 52, f"{w}/52 weeks with commits")
    else:
        cadence = _comp("Commit cadence", 40, None)

    if a.commits_last_year is not None:
        n = a.commits_last_year
        volume = _comp("Commit volume", 20, _log_points(n, 20, 100), f"{n} commits in the last year")
    else:
        volume = _comp("Commit volume", 20, None)

    return _metric(
        "development_activity",
        "Development activity",
        [recency, cadence, volume],
        {
            "days_since_last_push": a.days_since_last_push,
            "active_weeks_last_year": a.active_weeks_last_year,
            "commits_last_year": a.commits_last_year,
        },
    )


def metric_release_discipline(data: RepoData) -> Optional[Metric]:
    """Does the project ship versioned releases on a healthy cadence?"""
    a = data.activity
    if a.releases_count is None:
        return None

    if not a.releases_count:
        ships = _comp("Ships releases", 30, 0.0, "no releases published")
        recency = _comp("Release recency", 40, 0.0, "no releases")
        cadence = _comp("Release cadence", 30, 0.0, "no releases")
        return _metric(
            "release_discipline", "Release discipline", [ships, recency, cadence],
            {"releases_count": 0},
        )

    ships = _comp("Ships releases", 30, 30.0, f"{a.releases_count} releases published")

    if a.days_since_latest_release is not None:
        d = a.days_since_latest_release
        pts = 40.0 if d <= 90 else 30.0 if d <= 180 else 18.0 if d <= 365 else 8.0 if d <= 730 else 0.0
        recency = _comp("Release recency", 40, pts, f"latest release {d} days ago")
    else:
        recency = _comp("Release recency", 40, None)

    if a.mean_days_between_releases is not None:
        gap = a.mean_days_between_releases
        pts = 30.0 if gap <= 45 else 22.0 if gap <= 120 else 14.0 if gap <= 365 else 6.0
        cadence = _comp("Release cadence", 30, pts, f"a release every ~{gap:g} days")
    else:
        cadence = _comp("Release cadence", 30, 14.0, "cadence unknown (single release)")

    return _metric(
        "release_discipline",
        "Release discipline",
        [ships, recency, cadence],
        {
            "releases_count": a.releases_count,
            "days_since_latest_release": a.days_since_latest_release,
            "mean_days_between_releases": a.mean_days_between_releases,
            "latest_release_tag": a.latest_release_tag,
        },
    )


def metric_popularity(data: RepoData) -> Optional[Metric]:
    """How much adoption and attention does the project have?"""
    p = data.popularity
    stars = _comp("Stars", 60, _log_points(p.stars, 60, 5000), f"{p.stars:,} stars")
    forks = _comp("Forks", 25, _log_points(p.forks, 25, 1000), f"{p.forks:,} forks")
    watchers = _comp("Watchers", 15, _log_points(p.watchers, 15, 500), f"{p.watchers:,} watchers")
    return _metric(
        "popularity",
        "Popularity & adoption",
        [stars, forks, watchers],
        {"stars": p.stars, "forks": p.forks, "watchers": p.watchers},
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


def metric_stewardship(data: RepoData) -> Optional[Metric]:
    """Who stands behind this repo? Organization backing, reach, track record.

    This is where being owned by an organization (vs. a personal account)
    influences the score: an organization — especially one with a verified
    domain and reach — signals shared, accountable stewardship that outlives
    any single person.
    """
    owner = data.owner
    if owner is None:
        return None

    is_org = owner.type == "Organization"
    backing = _comp(
        "Ownership backing", 30, 30.0 if is_org else 10.0,
        "organization-owned" if is_org else "personal (user) account",
    )

    # Verified-domain badge only exists for organizations; N/A for users.
    if is_org:
        verified = _check("Verified domain", bool(owner.is_verified), 20)
    else:
        verified = _comp("Verified domain", 20, None, "not applicable to user accounts")

    reach = _comp(
        "Owner reach", 25, _log_points(owner.followers, 25, 3000),
        f"{owner.followers:,} followers of {owner.login}",
    )

    if owner.account_age_days is not None:
        age_years = owner.account_age_days / 365.25
        age_pts = min(12.0, age_years / 6 * 12.0)
    else:
        age_pts = None
    repos_pts = min(13.0, _log_points(owner.public_repos, 13, 60))
    track_pts = None if age_pts is None else age_pts + repos_pts
    track_detail = (
        f"{owner.public_repos} public repos"
        + (f", account ~{owner.account_age_days // 365} yr old" if owner.account_age_days else "")
    )
    track = _comp("Track record", 25, track_pts, track_detail)

    return _metric(
        "stewardship",
        "Ownership & stewardship",
        [backing, verified, reach, track],
        {
            "owner_login": owner.login,
            "owner_type": owner.type,
            "is_verified": owner.is_verified,
            "followers": owner.followers,
            "public_repos": owner.public_repos,
            "account_age_days": owner.account_age_days,
        },
    )


def metric_community_health(data: RepoData) -> Optional[Metric]:
    """Is the project set up to receive users and contributors?"""
    c = data.community
    checklist = [
        _check("README", c.has_readme, 25),
        _check("License", c.has_license, 25),
        _check("CONTRIBUTING guide", c.has_contributing, 20),
        _check("Code of conduct", c.has_code_of_conduct, 15),
        _check("Issue template", c.has_issue_template, 8),
        _check("PR template", c.has_pull_request_template, 7),
    ]
    return _metric(
        "community_health",
        "Community health",
        checklist,
        {
            "has_readme": c.has_readme,
            "has_license": c.has_license,
            "has_contributing": c.has_contributing,
            "has_code_of_conduct": c.has_code_of_conduct,
            "has_issue_template": c.has_issue_template,
            "has_pull_request_template": c.has_pull_request_template,
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
            "Linter config", q.has_linter_config, 20,
            ", ".join(q.linter_configs) if q.linter_configs else None,
        ),
        _check("Pre-commit hooks", q.has_precommit_config, 12),
        _check(".editorconfig", q.has_editorconfig, 8),
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
            "has_editorconfig": q.has_editorconfig,
        },
    )


def metric_documentation(data: RepoData) -> Optional[Metric]:
    """Can a newcomer find out what this is and how to use it?"""
    c = data.community
    q = data.quality_signals
    repo = data.repo
    checklist = [
        _check("README", c.has_readme, 30),
        _check("Documentation directory", q.has_docs_dir, 25),
        _check("Documentation / homepage site", bool(repo.homepage), 15, repo.homepage or None),
        _check("Repository description", c.has_description, 10),
        _check("Topics", bool(repo.topics), 10, f"{len(repo.topics)} topics" if repo.topics else None),
        _check("Wiki", bool(repo.has_wiki), 10),
    ]
    return _metric(
        "documentation",
        "Documentation",
        checklist,
        {
            "has_readme": c.has_readme,
            "has_docs_dir": q.has_docs_dir,
            "homepage": repo.homepage,
            "has_description": c.has_description,
            "topics": repo.topics,
            "has_wiki": repo.has_wiki,
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


# ---------------------------------------------------------------------------
# Category definitions & rollup
# ---------------------------------------------------------------------------


class CategorySpec:
    """One scoring category: a weighted group of metrics."""

    def __init__(
        self,
        key: str,
        name: str,
        description: str,
        weight: float,
        metrics: dict[str, tuple[float, Callable[[Any], Optional[Metric]]]],
    ):
        self.key = key
        self.name = name
        self.description = description
        self.weight = weight
        # metric_key -> (weight within category, compute function)
        self.metrics = metrics


# Repository categories. Category weights sum to 1.0.
REPO_CATEGORIES: list[CategorySpec] = [
    CategorySpec(
        "vitality", "Vitality",
        "Is the project alive — is code being written and are releases shipping?",
        0.22,
        {
            "development_activity": (0.6, metric_development_activity),
            "release_discipline": (0.4, metric_release_discipline),
        },
    ),
    CategorySpec(
        "community", "Community & Adoption",
        "Does the project have users, attention, and a welcoming setup for contributors?",
        0.18,
        {
            "popularity": (0.5, metric_popularity),
            "community_health": (0.5, metric_community_health),
        },
    ),
    CategorySpec(
        "governance", "Sustainability & Governance",
        "Will the project survive its people — bus factor, responsiveness, and who backs it?",
        0.24,
        {
            "maintainer_resilience": (0.4, metric_maintainer_resilience),
            "responsiveness": (0.3, metric_responsiveness),
            "stewardship": (0.3, metric_stewardship),
        },
    ),
    CategorySpec(
        "engineering", "Engineering Quality",
        "Are baseline engineering and documentation practices in place?",
        0.20,
        {
            "engineering_practices": (0.6, metric_engineering_practices),
            "documentation": (0.4, metric_documentation),
        },
    ),
    CategorySpec(
        "security", "Security",
        "Are visible security and supply-chain practices in place?",
        0.16,
        {"security_posture": (1.0, metric_security_posture)},
    ),
]


def _rollup(value_by_key: dict[str, int], weights: dict[str, float]) -> Optional[int]:
    available = {k: v for k, v in value_by_key.items() if k in weights}
    if not available:
        return None
    total = sum(weights[k] for k in available)
    return max(1, min(100, round(sum(v * weights[k] for k, v in available.items()) / total)))


def _build(
    specs: list[CategorySpec],
    computed: dict[str, Optional[Metric]],
    overall_name: str,
) -> tuple[Optional[Metric], list[MetricCategory]]:
    """Assemble categories and the overall score from computed metrics."""
    categories: list[MetricCategory] = []
    cat_values: dict[str, int] = {}
    cat_weights: dict[str, float] = {}

    for spec in specs:
        present = [
            computed[k] for k in spec.metrics if computed.get(k) is not None
        ]
        if not present:
            continue
        inner_weights = {k: w for k, (w, _) in spec.metrics.items()}
        value = _rollup({m.key: m.value for m in present}, inner_weights)
        categories.append(
            MetricCategory(
                key=spec.key,
                name=spec.name,
                description=spec.description,
                weight=spec.weight,
                value=value,
                band=band_for(value) if value is not None else None,
                metrics=present,
            )
        )
        if value is not None:
            cat_values[spec.key] = value
            cat_weights[spec.key] = spec.weight

    overall_value = _rollup(cat_values, cat_weights)
    if overall_value is None:
        return None, categories
    dropped = [s.name for s in specs if s.key not in cat_values]
    overall = Metric(
        key="overall",
        name=overall_name,
        value=overall_value,
        band=band_for(overall_value),
        inputs={k: v for k, v in cat_values.items()},
        note=f"Categories without data excluded and weights renormalized: {', '.join(dropped)}"
        if dropped
        else None,
    )
    return overall, categories


def compute_metrics(data: RepoData) -> Metrics:
    computed: dict[str, Optional[Metric]] = {}
    for spec in REPO_CATEGORIES:
        for key, (_, fn) in spec.metrics.items():
            computed[key] = fn(data)
    overall, categories = _build(REPO_CATEGORIES, computed, "Overall health")
    return Metrics(metrics_version=METRICS_VERSION, overall=overall, categories=categories)


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
        "Portfolio size", 15, _log_points(info.public_repos, 15, 100),
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
        "Followers", 50, _log_points(info.followers, 50, 1000),
        f"{info.followers} followers",
    )
    stars = _comp(
        "Stars across repositories", 50, _log_points(p.total_stars_sampled, 50, 10000),
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


# Organization categories. Category weights sum to 1.0.
ORG_CATEGORIES: list[CategorySpec] = [
    CategorySpec(
        "activity_reach", "Activity & Reach",
        "Is the repository portfolio maintained and does the org have traction?",
        0.75,
        {
            "portfolio_activity": (0.6, metric_org_portfolio),
            "community_reach": (0.4, metric_org_reach),
        },
    ),
    CategorySpec(
        "governance", "Governance & Profile",
        "Is the organization accountable and clearly presented?",
        0.25,
        {"profile_completeness": (1.0, metric_org_profile)},
    ),
]


def compute_org_metrics(data: OrgData) -> OrgMetrics:
    computed: dict[str, Optional[Metric]] = {}
    for spec in ORG_CATEGORIES:
        for key, (_, fn) in spec.metrics.items():
            computed[key] = fn(data)
    overall, categories = _build(ORG_CATEGORIES, computed, "Overall organization health")
    return OrgMetrics(metrics_version=METRICS_VERSION, overall=overall, categories=categories)
