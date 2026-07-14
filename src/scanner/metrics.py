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
    ScanConfig,
    Scorecard,
)
from .scorecard import check_weight

METRICS_VERSION = "1.2.0"

# A source file above this size (bytes, ~1,500 lines) strains an agent's
# working context; used by the AI code-legibility metric.
AI_OVERSIZED_SOURCE_BYTES = 60_000
# An agent-instruction file smaller than this is treated as a stub, not
# substantive guidance (anti-gaming for the AI agent-context metric).
AI_AGENT_STUB_BYTES = 200
# Languages whose type systems are checkable out of the box (AI legibility).
STATICALLY_TYPED_LANGUAGES = {
    "TypeScript", "Go", "Rust", "Java", "Kotlin", "C#", "Scala", "Swift",
    "C++", "C", "Haskell", "OCaml", "F#", "Elm", "Dart",
}

# Detail string marking a component excluded because the scan configuration
# switched it off (as opposed to missing data). Used to phrase metric notes.
DISABLED_DETAIL = "disabled in scan configuration"
SCORECARD_UNAVAILABLE_DETAIL = "OpenSSF Scorecard unavailable"

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


def _log_points(
    value: int,
    max_points: float,
    saturating_value: float,
    threshold: int = 0,
) -> float:
    """Log-scaled points: 0 at or below ``threshold``, ``max_points`` at ``saturating_value``.

    ``threshold`` is the highest value that still earns nothing; scoring ramps up
    from the next value. With the default of 0, any positive value earns points.
    """
    if value <= threshold:
        return 0.0
    scale = max_points / math.log10(saturating_value - threshold + 1)
    return min(max_points, math.log10(value - threshold + 1) * scale)


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


def _scorecard_evidence(data: RepoData, check_name: str, weight: float) -> MetricComponent:
    """Return one cross-category OpenSSF Scorecard component.

    Scorecard remains the complete, risk-weighted Security metric.  A small
    number of checks also evidence a different health dimension (for example,
    CI-Tests evidences engineering practice).  Those cards use a deliberately
    small, category-specific weight -- never Scorecard's security risk weight.
    Missing or inconclusive Scorecard evidence is excluded, like every other
    unavailable input, rather than treated as a failure.
    """
    label = f"OpenSSF Scorecard: {check_name}"
    scorecard = data.security_signals.scorecard
    if scorecard is None:
        return _comp(label, weight, None, SCORECARD_UNAVAILABLE_DETAIL)
    check = next((item for item in scorecard.checks if item.name == check_name), None)
    if check is None:
        return _comp(label, weight, None, "not reported by this Scorecard version")
    if check.score is None:
        return _comp(label, weight, None, check.reason or "inconclusive")
    return _comp(label, weight, check.score / 10.0 * weight, check.reason)


def _license_component(data: RepoData, weight: float) -> MetricComponent:
    """Single license signal for community health.

    License presence is detected once, by OpenSSF Scorecard's ``License`` check
    -- a published, tool-neutral test for a recognized license file, graded
    0..10.  When Scorecard is unavailable or inconclusive we fall back to
    GitHub's community-profile license flag, so a repository without a Scorecard
    still earns a license signal.  The component is labelled generically as
    "License"; the Scorecard provenance is documented in the methodology, not
    surfaced on the card, so this reads as one license row rather than two.
    """
    scorecard = data.security_signals.scorecard
    if scorecard is not None:
        check = next((item for item in scorecard.checks if item.name == "License"), None)
        if check is not None and check.score is not None:
            return _comp("License", weight, check.score / 10.0 * weight, check.reason)
    return _check("License", data.community.has_license, weight)


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
    excluded = [c for c in components if c.status == "excluded"]
    disabled = [c.name for c in excluded if c.detail == DISABLED_DETAIL]
    no_data = [
        c.name for c in excluded
        if c.detail not in (DISABLED_DETAIL, SCORECARD_UNAVAILABLE_DETAIL)
    ]
    note_parts: list[str] = []
    if no_data:
        note_parts.append(f"Excluded from scoring (no data or not applicable): {', '.join(no_data)}.")
    if disabled:
        note_parts.append(f"Disabled in scan configuration: {', '.join(disabled)}.")
    if any(c.detail != SCORECARD_UNAVAILABLE_DETAIL for c in excluded):
        note_parts.append("Remaining weights renormalized.")
    return Metric(
        key=key,
        name=name,
        value=value,
        band=band_for(value),
        components=components,
        inputs=inputs,
        note=" ".join(note_parts) if note_parts else None,
    )


def _disable_components(metric: Metric, disabled_names: set[str]) -> Optional[Metric]:
    """Re-score a metric with the named components switched off by configuration.

    Each named component becomes ``excluded`` and its weight is renormalized
    away, mirroring how missing data is handled. Returns None if switching them
    off leaves the metric with nothing scorable."""
    if not disabled_names:
        return metric
    updated: list[MetricComponent] = []
    changed = False
    for c in metric.components:
        if c.name in disabled_names and c.status != "excluded":
            updated.append(
                MetricComponent(
                    name=c.name,
                    points=0.0,
                    max_points=c.max_points,
                    status="excluded",
                    detail=DISABLED_DETAIL,
                )
            )
            changed = True
        else:
            updated.append(c)
    if not changed:
        return metric
    return _metric(metric.key, metric.name, updated, metric.inputs)


# ---------------------------------------------------------------------------
# Repository metrics
# ---------------------------------------------------------------------------


def metric_development_activity(data: RepoData) -> Optional[Metric]:
    """Is code actively being written? (push recency, cadence, volume)"""
    a = data.activity

    if a.days_since_last_push is not None:
        d = a.days_since_last_push
        pts = 40.0 if d <= 7 else 32.0 if d <= 30 else 20.0 if d <= 90 else 11.0 if d <= 180 else 4.0 if d <= 365 else 0.0
        recency = _comp("Push recency", 36, pts / 40 * 36, f"last push {d} days ago")
    else:
        recency = _comp("Push recency", 36, None)

    if a.active_weeks_last_year is not None:
        w = min(a.active_weeks_last_year, 52)
        cadence = _comp("Commit cadence", 36, 36.0 * w / 52, f"{w}/52 weeks with commits")
    else:
        cadence = _comp("Commit cadence", 36, None)

    if a.commits_last_year is not None:
        n = a.commits_last_year
        volume = _comp("Commit volume", 18, _log_points(n, 18, 100), f"{n} commits in the last year")
    else:
        volume = _comp("Commit volume", 18, None)

    return _metric(
        "development_activity",
        "Development activity",
        [recency, cadence, volume, _scorecard_evidence(data, "Maintained", 10)],
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
        ships = _comp("Ships releases", 27, 0.0, "no releases published")
        recency = _comp("Release recency", 36, 0.0, "no releases")
        cadence = _comp("Release cadence", 27, 0.0, "no releases")
        return _metric(
            "release_discipline", "Release discipline", [
                ships, recency, cadence, _scorecard_evidence(data, "Signed-Releases", 10),
            ],
            {"releases_count": 0},
        )

    ships = _comp("Ships releases", 27, 27.0, f"{a.releases_count} releases published")

    if a.days_since_latest_release is not None:
        d = a.days_since_latest_release
        pts = 36.0 if d <= 90 else 27.0 if d <= 180 else 16.2 if d <= 365 else 7.2 if d <= 730 else 0.0
        recency = _comp("Release recency", 36, pts, f"latest release {d} days ago")
    else:
        recency = _comp("Release recency", 36, None)

    if a.mean_days_between_releases is not None:
        gap = a.mean_days_between_releases
        pts = 27.0 if gap <= 45 else 19.8 if gap <= 120 else 12.6 if gap <= 365 else 5.4
        cadence = _comp("Release cadence", 27, pts, f"a release every ~{gap:g} days")
    else:
        cadence = _comp("Release cadence", 27, 12.6, "cadence unknown (single release)")

    return _metric(
        "release_discipline",
        "Release discipline",
        [ships, recency, cadence, _scorecard_evidence(data, "Signed-Releases", 10)],
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
    stars = _comp("Stars", 60, _log_points(p.stars, 60, 5000, threshold=2), f"{p.stars:,} stars")
    forks = _comp("Forks", 25, _log_points(p.forks, 25, 1000, threshold=2), f"{p.forks:,} forks")
    watchers = _comp("Watchers", 15, _log_points(p.watchers, 15, 500, threshold=2), f"{p.watchers:,} watchers")
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
    bf_pts = {1: 9.0, 2: 25.2, 3: 36.0, 4: 43.2}.get(bf, min(54.0, 43.2 + (bf - 4) * 2.7))
    bus = _comp("Bus factor", 54, bf_pts, f"{bf} contributor(s) cover half of all commits")

    if m.top_contributor_share is not None:
        share = m.top_contributor_share
        distribution = _comp(
            "Commit distribution", 22.5, (1.0 - share) * 22.5,
            f"top contributor authored {share:.0%} of commits",
        )
    else:
        distribution = _comp("Commit distribution", 22.5, None)

    if m.contributors_sampled is not None:
        breadth = _comp(
            "Contributor breadth", 13.5, min(13.5, m.contributors_sampled * 1.35),
            f"{m.contributors_sampled} contributors",
        )
    else:
        breadth = _comp("Contributor breadth", 13.5, None)

    return _metric(
        "maintainer_resilience",
        "Maintainer resilience (bus factor)",
        [bus, distribution, breadth, _scorecard_evidence(data, "Contributors", 10)],
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
            "Issue resolution", 46.75, issues.closed_ratio * 46.75,
            f"{issues.closed_ratio:.0%} of issues closed",
        )
    else:
        issue_component = _comp("Issue resolution", 46.75, None, "no issues or no data")

    pr_component = _comp("PR acceptance", 38.25, None, "no decided pull requests or no data")
    if issues.merged_prs is not None and issues.closed_unmerged_prs is not None:
        decided = issues.merged_prs + issues.closed_unmerged_prs
        if decided > 0:
            pr_component = _comp(
                "PR acceptance", 38.25, issues.merged_prs / decided * 38.25,
                f"{issues.merged_prs}/{decided} decided PRs merged",
            )

    return _metric(
        "responsiveness",
        "Issue & PR responsiveness",
        [issue_component, pr_component, _scorecard_evidence(data, "Code-Review", 15)],
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
        _check("README", c.has_readme, 22.5),
        _license_component(data, 22.5),
        _check("CONTRIBUTING guide", c.has_contributing, 18),
        _check("Code of conduct", c.has_code_of_conduct, 13.5),
        _check("Issue template", c.has_issue_template, 7.2),
        _check("PR template", c.has_pull_request_template, 6.3),
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
            "CI workflows", q.has_ci, 24,
            f"{len(q.ci_workflows)} workflow(s)" if q.has_ci else None,
        ),
        _check("Tests present", q.has_tests, 24),
        _check(
            "Linter config", q.has_linter_config, 16,
            ", ".join(q.linter_configs) if q.linter_configs else None,
        ),
        _check("Pre-commit hooks", q.has_precommit_config, 9.6),
        _check(".editorconfig", q.has_editorconfig, 6.4),
        _scorecard_evidence(data, "CI-Tests", 20),
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


def _scored_packages(data: RepoData) -> list:
    """Published packages that belong to this repo (registry repo URL matches,
    or the registry declares none — benefit of the doubt since the manifest is
    in the repo)."""
    return [
        p for p in data.ecosystem.packages if p.exists and p.matches_repo is not False
    ]


def metric_ecosystem_adoption(data: RepoData) -> Optional[Metric]:
    """How widely is the published package actually installed?

    Real download counts from the package registry are a far stronger adoption
    signal than GitHub stars — people install libraries they never star.
    ``None`` for repos that publish nothing (or when no download data is
    available), so non-package repos are never penalized.
    """
    packages = _scored_packages(data)
    if not packages:
        return None

    monthly_values = [p.monthly_downloads for p in packages if p.monthly_downloads is not None]
    total_values = [p.total_downloads for p in packages if p.total_downloads is not None]
    dependents_values = [p.dependents_count for p in packages if p.dependents_count is not None]
    if not monthly_values and not total_values and not dependents_values:
        return None

    ecosystems = ", ".join(sorted({p.ecosystem for p in packages}))
    # Prefer monthly downloads; fall back to lifetime totals for registries that
    # publish no monthly figure (e.g. RubyGems), at a higher saturation point.
    if monthly_values:
        monthly = sum(monthly_values)
        downloads = _comp(
            "Monthly downloads", 80, _log_points(monthly, 80, 1_000_000),
            f"{monthly:,} downloads/month across {ecosystems}",
        )
    elif total_values:
        total = sum(total_values)
        downloads = _comp(
            "Total downloads", 80, _log_points(total, 80, 50_000_000),
            f"{total:,} downloads all-time across {ecosystems}",
        )
    else:
        downloads = _comp("Downloads", 80, None, "download stats unavailable")

    if dependents_values:
        dependents = sum(dependents_values)
        dep = _comp(
            "Registry dependents", 20, _log_points(dependents, 20, 1000),
            f"{dependents:,} packages depend on it",
        )
    else:
        dep = _comp("Registry dependents", 20, None, "not reported by this ecosystem")

    return _metric(
        "ecosystem_adoption",
        "Ecosystem adoption (downloads)",
        [downloads, dep],
        {
            "ecosystems": ecosystems,
            "monthly_downloads": sum(monthly_values) if monthly_values else None,
            "total_downloads": sum(total_values) if total_values else None,
            "dependents": sum(dependents_values) if dependents_values else None,
            "packages": [p.name for p in packages],
        },
    )


def metric_package_maintenance(data: RepoData) -> Optional[Metric]:
    """Is the published package current, versioned, and not deprecated?

    Registry publish recency and deprecation/abandonment are distinct from
    GitHub activity — a library can go stale or be marked abandoned on the
    registry while its repo still sees commits, and vice versa.
    """
    packages = _scored_packages(data)
    if not packages:
        return None

    ecosystems = ", ".join(sorted({p.ecosystem for p in packages}))
    published = _comp(
        "Published & resolvable", 25, 25.0,
        f"{len(packages)} package(s) on {ecosystems}",
    )

    recency_days = [p.days_since_latest_publish for p in packages
                    if p.days_since_latest_publish is not None]
    if recency_days:
        d = min(recency_days)
        pts = 35.0 if d <= 180 else 26.0 if d <= 365 else 14.0 if d <= 730 else 4.0
        recency = _comp("Publish recency", 35, pts, f"latest publish {d} days ago")
    else:
        recency = _comp("Publish recency", 35, None)

    version_counts = [p.versions_count for p in packages if p.versions_count is not None]
    if version_counts:
        n = max(version_counts)
        pts = 20.0 if n >= 5 else 12.0 if n >= 2 else 4.0
        history = _comp("Version history", 20, pts, f"{n} published versions")
    else:
        history = _comp("Version history", 20, None)

    deprecated = [p for p in packages if p.is_deprecated or p.latest_version_yanked]
    if deprecated:
        note = deprecated[0].deprecation_note or "deprecated/yanked on the registry"
        health = _comp("Not deprecated", 20, 0.0, f"{deprecated[0].name}: {note}")
    else:
        health = _comp("Not deprecated", 20, 20.0, "active, not deprecated or yanked")

    return _metric(
        "package_maintenance",
        "Package maintenance",
        [published, recency, history, health],
        {
            "ecosystems": ecosystems,
            "packages": [p.name for p in packages],
            "min_days_since_publish": min(recency_days) if recency_days else None,
            "any_deprecated": bool(deprecated),
        },
    )


def _security_from_scorecard(sc: Scorecard) -> Optional[Metric]:
    """Security posture from OpenSSF Scorecard checks.

    Each check becomes a component weighted by Scorecard's own risk level, so
    the rolled-up value tracks Scorecard's aggregate. A check Scorecard could
    not determine (``score is None``, i.e. it returned -1) is excluded and its
    weight renormalized away — never scored as zero. This is what makes the
    metric tool-agnostic: any accepted SAST / dependency-update / signing tool
    earns the relevant check, and undetectable practices don't tank the score.
    """
    components: list[MetricComponent] = []
    for check in sc.checks:
        weight = check_weight(check.name)
        if check.score is None:
            components.append(_comp(check.name, weight, None, check.reason or "inconclusive"))
        else:
            components.append(
                _comp(check.name, weight, check.score / 10.0 * weight, check.reason)
            )
    return _metric(
        "security_posture",
        "Security posture",
        components,
        {
            "source": "openssf_scorecard",
            "scorecard_aggregate": sc.aggregate_score,
            "scorecard_version": sc.scorecard_version,
            "checks_evaluated": sum(1 for c in sc.checks if c.score is not None),
            "checks_inconclusive": sum(1 for c in sc.checks if c.score is None),
        },
    )


def _security_from_files(data: RepoData) -> Optional[Metric]:
    """Fallback security posture from file-tree signals, used when OpenSSF
    Scorecard is unavailable. Coarser and more vendor-specific than Scorecard —
    kept only so a report still carries a security signal without the CLI."""
    s = data.security_signals

    # A committed lockfile pins dependencies — but that is an *application*
    # concern. A published library/gem is expected NOT to commit one (Bundler
    # tells gem authors not to check in Gemfile.lock; npm/PyPI libraries specify
    # ranges too), so scoring its absence would penalize every well-behaved
    # package. Only score lockfiles for repos that declare dependencies AND do
    # not publish a package; otherwise exclude and renormalize.
    if not data.dependencies.manifests:
        lockfiles = _comp("Dependency lockfiles", 25, None, "no dependency manifests — not applicable")
    elif _scored_packages(data):
        lockfiles = _comp(
            "Dependency lockfiles", 25, None,
            "published library — lockfiles are an application concern, not expected",
        )
    else:
        lockfiles = _check(
            "Dependency lockfiles", bool(s.lockfiles), 25,
            ", ".join(s.lockfiles) if s.lockfiles else None,
        )

    components = [
        _check("Security policy (SECURITY.md)", s.has_security_policy, 30),
        _check("Dependabot config", s.has_dependabot_config, 25),
        lockfiles,
        _check("CodeQL workflow", s.has_codeql_workflow, 20),
    ]
    return _metric(
        "security_posture",
        "Security posture",
        components,
        {
            "source": "file_signals",
            "has_security_policy": s.has_security_policy,
            "has_dependabot_config": s.has_dependabot_config,
            "has_codeql_workflow": s.has_codeql_workflow,
            "lockfiles": s.lockfiles,
            "manifests": data.dependencies.manifests,
        },
    )


def metric_security_posture(data: RepoData) -> Optional[Metric]:
    """Security posture — OpenSSF Scorecard when available, file checks otherwise.

    Scorecard is a neutral, tool-agnostic standard; we prefer it over detecting
    specific vendor config files. When the Scorecard CLI didn't run (not
    installed, failed, or disabled), we fall back to coarse file-tree signals.
    """
    sc = data.security_signals.scorecard
    if sc is not None:
        metric = _security_from_scorecard(sc)
        if metric is not None:
            return metric
    return _security_from_files(data)


# ---------------------------------------------------------------------------
# AI readiness metrics
#
# How well the repo is equipped to be developed and maintained with AI coding
# agents. Presence-based, file-tree signals — infrastructure exists, not how
# good it is. The category carries weight 0.0 in the overall score: it is an
# independent, additive badge that never drags a solid project's health down.
# ---------------------------------------------------------------------------


def metric_ai_agent_context(data: RepoData) -> Optional[Metric]:
    """Does the repo give AI agents guidance and machine-readable docs?"""
    ai = data.ai_readiness

    if ai.agent_instruction_files:
        substantive = (ai.agent_instruction_max_bytes or 0) >= AI_AGENT_STUB_BYTES
        pts = 60.0 if substantive else 24.0
        detail = ", ".join(ai.agent_instruction_files)
        if not substantive:
            detail += " (stub)"
        instructions = _comp("Agent instructions", 60, pts, detail)
    else:
        instructions = _comp("Agent instructions", 60, 0.0, "no CLAUDE.md / AGENTS.md / editor rules")

    llms = _check(
        "Machine-readable docs (llms.txt)", ai.has_llms_txt, 40,
        "llms.txt present" if ai.has_llms_txt else None,
    )
    return _metric(
        "ai_agent_context",
        "Agent context & guidance",
        [instructions, llms],
        {
            "agent_instruction_files": ai.agent_instruction_files,
            "agent_instruction_max_bytes": ai.agent_instruction_max_bytes,
            "has_llms_txt": ai.has_llms_txt,
        },
    )


def metric_ai_verify_loop(data: RepoData) -> Optional[Metric]:
    """Can an agent set up, run, and verify a change autonomously?

    The single most important dimension for autonomous agents — hence the
    highest weight in the category. Reuses the canonical test/lint/lockfile
    signals plus AI-specific bootstrap, type-check and container signals.
    """
    ai = data.ai_readiness
    q = data.quality_signals
    lockfiles = data.security_signals.lockfiles

    bootstrap = _check(
        "One-command bootstrap", bool(ai.bootstrap_files), 22.5,
        ", ".join(ai.bootstrap_files) if ai.bootstrap_files else None,
    )
    tests = _check("Automated tests", q.has_tests, 27)
    lint = _check(
        "Lint / format config", q.has_linter_config, 13.5,
        ", ".join(q.linter_configs) if q.linter_configs else None,
    )

    typed_language = data.repo.primary_language in STATICALLY_TYPED_LANGUAGES
    has_typecheck = bool(ai.typecheck_configs) or typed_language
    typecheck_detail = (
        ", ".join(ai.typecheck_configs) if ai.typecheck_configs
        else f"{data.repo.primary_language} (statically typed)" if typed_language
        else None
    )
    typecheck = _check("Static type checking", has_typecheck, 13.5, typecheck_detail)

    repro_bits = []
    if ai.has_devcontainer:
        repro_bits.append("devcontainer")
    if ai.has_dockerfile:
        repro_bits.append("Dockerfile")
    if ai.has_nix:
        repro_bits.append("Nix")
    if lockfiles:
        repro_bits.append("lockfile")
    repro = _check(
        "Reproducible environment", bool(repro_bits), 13.5,
        ", ".join(repro_bits) if repro_bits else None,
    )

    return _metric(
        "ai_verify_loop",
        "Verify loop (build / test / typecheck)",
        [
            bootstrap, tests, lint, typecheck, repro,
            _scorecard_evidence(data, "Pinned-Dependencies", 10),
        ],
        {
            "bootstrap_files": ai.bootstrap_files,
            "has_tests": q.has_tests,
            "has_linter_config": q.has_linter_config,
            "typecheck_configs": ai.typecheck_configs,
            "typed_language": typed_language,
            "has_devcontainer": ai.has_devcontainer,
            "has_dockerfile": ai.has_dockerfile,
            "has_nix": ai.has_nix,
            "lockfiles": lockfiles,
        },
    )


def metric_ai_code_legibility(data: RepoData) -> Optional[Metric]:
    """Is the code legible to a model? (typed, and no giant files)

    ``None`` for repos with no detected source files (docs-only, etc.), so
    they are never penalized for a dimension that does not apply."""
    ai = data.ai_readiness
    lang = data.repo.primary_language

    if lang is not None:
        typed_language = lang in STATICALLY_TYPED_LANGUAGES
        if typed_language:
            type_pts, type_detail = 45.0, f"{lang} (statically typed)"
        elif ai.typecheck_configs:
            type_pts = 27.0
            type_detail = f"{lang} with type-check config ({', '.join(ai.typecheck_configs)})"
        else:
            type_pts, type_detail = 0.0, f"{lang} without a type-check config"
        typing = _comp("Type-checkable code", 45, type_pts, type_detail)
    else:
        typing = _comp("Type-checkable code", 45, None, "primary language unknown")

    if ai.source_files_sampled > 0:
        share_ok = 1.0 - ai.oversized_source_files / ai.source_files_sampled
        file_sizes = _comp(
            "Manageable file sizes", 55, share_ok * 55.0,
            f"{ai.oversized_source_files}/{ai.source_files_sampled} source files over "
            f"{AI_OVERSIZED_SOURCE_BYTES // 1000}KB",
        )
    else:
        file_sizes = _comp("Manageable file sizes", 55, None, "no source files detected")

    metric = _metric(
        "ai_code_legibility",
        "Code legibility for models",
        [typing, file_sizes],
        {
            "primary_language": lang,
            "source_files_sampled": ai.source_files_sampled,
            "oversized_source_files": ai.oversized_source_files,
            "largest_source_bytes": ai.largest_source_bytes,
        },
    )
    return metric


def metric_ai_interfaces(data: RepoData) -> Optional[Metric]:
    """Does the repo expose machine-readable interfaces and runnable examples?

    ``None`` when the repo exposes none of these — a plain library legitimately
    has no API schema, so absence is treated as not-applicable (excluded and
    renormalized), never as a penalty."""
    ai = data.ai_readiness
    if not (ai.api_schema_files or ai.has_mcp_signal or ai.example_dirs):
        return None

    schema = _check(
        "API schema (OpenAPI/GraphQL/proto)", bool(ai.api_schema_files), 40,
        ", ".join(ai.api_schema_files) if ai.api_schema_files else None,
    )
    mcp = _check("MCP server", ai.has_mcp_signal, 20)
    examples = _check(
        "Runnable examples", bool(ai.example_dirs), 40,
        ", ".join(ai.example_dirs) if ai.example_dirs else None,
    )
    return _metric(
        "ai_interfaces",
        "Machine-readable interfaces",
        [schema, mcp, examples],
        {
            "api_schema_files": ai.api_schema_files,
            "has_mcp_signal": ai.has_mcp_signal,
            "example_dirs": ai.example_dirs,
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
        "Does the project have users, downloads, attention, and a welcoming setup for contributors?",
        0.18,
        {
            "popularity": (0.4, metric_popularity),
            "community_health": (0.35, metric_community_health),
            "ecosystem_adoption": (0.25, metric_ecosystem_adoption),
        },
    ),
    CategorySpec(
        "governance", "Sustainability & Governance",
        "Will the project survive its people — bus factor, responsiveness, who backs it, and package upkeep?",
        0.24,
        {
            "maintainer_resilience": (0.3, metric_maintainer_resilience),
            "responsiveness": (0.25, metric_responsiveness),
            "stewardship": (0.25, metric_stewardship),
            "package_maintenance": (0.2, metric_package_maintenance),
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
    CategorySpec(
        "ai_readiness", "AI Readiness",
        "How well is the repo equipped to be developed and maintained with AI "
        "coding agents? An independent, experimental badge — weight 0.0, so it "
        "is surfaced on its own and does not affect the overall health score.",
        0.0,
        {
            "ai_agent_context": (0.30, metric_ai_agent_context),
            "ai_verify_loop": (0.40, metric_ai_verify_loop),
            "ai_code_legibility": (0.15, metric_ai_code_legibility),
            "ai_interfaces": (0.15, metric_ai_interfaces),
        },
    ),
]


def _rollup(value_by_key: dict[str, int], weights: dict[str, float]) -> Optional[int]:
    available = {k: v for k, v in value_by_key.items() if k in weights}
    if not available:
        return None
    total = sum(weights[k] for k in available)
    # All available items carry zero weight (e.g. only the weight-0 AI Readiness
    # category is present) — no weighted mean is defined, so report no score.
    if total <= 0:
        return None
    return max(1, min(100, round(sum(v * weights[k] for k, v in available.items()) / total)))


def _build(
    specs: list[CategorySpec],
    computed: dict[str, Optional[Metric]],
    overall_name: str,
    config: ScanConfig,
) -> tuple[Optional[Metric], list[MetricCategory]]:
    """Assemble categories and the overall score from computed metrics.

    Categories disabled by ``config`` are dropped entirely (not rendered, not
    scored); categories with no scorable metric are dropped as before."""
    categories: list[MetricCategory] = []
    cat_values: dict[str, int] = {}
    cat_weights: dict[str, float] = {}

    for spec in specs:
        if not config.category_enabled(spec.key):
            continue
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
        # Weight-0 categories (e.g. AI Readiness) are rendered on their own but
        # excluded from the overall score and its inputs/notes — they are
        # independent, additive badges, not part of the health rollup.
        if value is not None and spec.weight > 0:
            cat_values[spec.key] = value
            cat_weights[spec.key] = spec.weight

    overall_value = _rollup(cat_values, cat_weights)
    if overall_value is None:
        return None, categories
    disabled = [s.name for s in specs if not config.category_enabled(s.key)]
    no_data = [
        s.name for s in specs
        if config.category_enabled(s.key) and s.weight > 0 and s.key not in cat_values
    ]
    note_parts: list[str] = []
    if no_data:
        note_parts.append(f"Categories without data excluded: {', '.join(no_data)}.")
    if disabled:
        note_parts.append(f"Categories disabled in scan configuration: {', '.join(disabled)}.")
    if disabled or no_data:
        note_parts.append("Weights renormalized over the remaining categories.")
    overall = Metric(
        key="overall",
        name=overall_name,
        value=overall_value,
        band=band_for(overall_value),
        inputs={k: v for k, v in cat_values.items()},
        note=" ".join(note_parts) if note_parts else None,
    )
    return overall, categories


def _compute(
    specs: list[CategorySpec], data: Any, config: ScanConfig
) -> dict[str, Optional[Metric]]:
    """Run each metric function, honoring the scan configuration.

    Disabled categories and metrics are skipped (recorded as None); enabled
    metrics have their configuration-disabled components switched off."""
    computed: dict[str, Optional[Metric]] = {}
    for spec in specs:
        category_on = config.category_enabled(spec.key)
        for key, (_, fn) in spec.metrics.items():
            if not category_on or not config.metric_enabled(key):
                computed[key] = None
                continue
            metric = fn(data)
            if metric is not None:
                metric = _disable_components(metric, config.disabled_component_names(key))
            computed[key] = metric
    return computed


def compute_metrics(data: RepoData, config: Optional[ScanConfig] = None) -> Metrics:
    config = config or ScanConfig()
    computed = _compute(REPO_CATEGORIES, data, config)
    overall, categories = _build(REPO_CATEGORIES, computed, "Overall health", config)
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


def compute_org_metrics(data: OrgData, config: Optional[ScanConfig] = None) -> OrgMetrics:
    config = config or ScanConfig()
    computed = _compute(ORG_CATEGORIES, data, config)
    overall, categories = _build(ORG_CATEGORIES, computed, "Overall organization health", config)
    return OrgMetrics(metrics_version=METRICS_VERSION, overall=overall, categories=categories)


# ---------------------------------------------------------------------------
# Configuration registry & validation
# ---------------------------------------------------------------------------

# Repository and organization methodologies share the same key namespace for
# validation — a repository-only key is still "known" when scanning an org.
ALL_CATEGORIES: list[CategorySpec] = REPO_CATEGORIES + ORG_CATEGORIES


def known_category_keys() -> set[str]:
    """Every category key the methodology defines (repositories + organizations)."""
    return {c.key for c in ALL_CATEGORIES}


def known_metric_keys() -> set[str]:
    """Every metric key the methodology defines (repositories + organizations)."""
    return {k for c in ALL_CATEGORIES for k in c.metrics}


def validate_config(config: ScanConfig) -> list[str]:
    """Return human-readable warnings for keys the methodology doesn't define.

    Category and metric keys are checked against the combined methodology.
    Component names are scoped to their metric and are not strictly validated —
    an unrecognized component name simply has no effect (only the parent metric
    key is checked)."""
    warnings: list[str] = []
    categories, metrics = known_category_keys(), known_metric_keys()
    for key in config.disabled_categories:
        if key not in categories:
            warnings.append(f"unknown category '{key}' in scan configuration (ignored)")
    for key in config.disabled_metrics:
        if key not in metrics:
            warnings.append(f"unknown metric '{key}' in scan configuration (ignored)")
    for key in config.disabled_components:
        if key not in metrics:
            warnings.append(
                f"disabled_components references unknown metric '{key}' (ignored)"
            )
    return warnings
