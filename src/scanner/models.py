"""Pydantic models for the scanner JSON report.

The report has two distinct layers:

- ``data``    — raw facts collected from public sources (GitHub API). No
                judgement, no scoring; values are reported as observed.
- ``metrics`` — standardized scores (integers 1..100) computed from ``data``
                by a versioned, transparent methodology (see ``metrics.py``
                and docs/metrics.md).

Schema and metrics methodology are versioned independently:
``Report.schema_version`` covers the JSON structure; ``Metrics.metrics_version``
covers the scoring formulas. Any breaking change must bump the respective
version — downstream scoring/certification depends on a stable schema, and
trust depends on a transparent methodology.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "0.3.0"

# ---------------------------------------------------------------------------
# Data layer: raw observed facts
# ---------------------------------------------------------------------------


class RepoRef(BaseModel):
    """Identity of the scanned repository."""

    url: str
    host: str = "github.com"
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class RepoInfo(BaseModel):
    """Basic repository metadata."""

    description: Optional[str] = None
    homepage: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    pushed_at: Optional[datetime] = None
    default_branch: Optional[str] = None
    is_fork: bool = False
    is_archived: bool = False
    is_disabled: bool = False
    size_kb: Optional[int] = None
    primary_language: Optional[str] = None
    languages: dict[str, int] = Field(
        default_factory=dict,
        description="Language name -> bytes of code, from the GitHub languages API",
    )
    topics: list[str] = Field(default_factory=list)
    license_spdx: Optional[str] = Field(
        default=None, description="SPDX identifier of the detected license, if any"
    )


class Popularity(BaseModel):
    stars: int = 0
    forks: int = 0
    watchers: int = Field(default=0, description="Subscribers (users watching for notifications)")
    open_issues_and_prs: int = Field(
        default=0, description="GitHub's combined open issues + open PRs counter"
    )


class Contributor(BaseModel):
    login: str
    commits: int


class Activity(BaseModel):
    """Commit / release activity facts."""

    commits_last_year: Optional[int] = Field(
        default=None, description="Total commits in the last 52 weeks (all contributors)"
    )
    active_weeks_last_year: Optional[int] = Field(
        default=None, description="Number of weeks with at least one commit in the last 52 weeks"
    )
    days_since_last_push: Optional[int] = None
    releases_count: Optional[int] = Field(
        default=None, description="Number of releases fetched (capped at 100)"
    )
    latest_release_tag: Optional[str] = None
    latest_release_at: Optional[datetime] = None
    mean_days_between_releases: Optional[float] = Field(
        default=None, description="Mean gap between the most recent releases (up to 10)"
    )


class IssueMetrics(BaseModel):
    open_issues: Optional[int] = None
    closed_issues: Optional[int] = None
    closed_ratio: Optional[float] = Field(
        default=None, description="closed / (open + closed), None when the repo has no issues"
    )
    open_prs: Optional[int] = None
    merged_prs: Optional[int] = None
    closed_unmerged_prs: Optional[int] = None


class Maintainership(BaseModel):
    contributors_sampled: Optional[int] = Field(
        default=None, description="Contributors counted (capped at 100 by the API page size)"
    )
    top_contributors: list[Contributor] = Field(default_factory=list)
    bus_factor: Optional[int] = Field(
        default=None,
        description="Smallest number of contributors whose commits cover >=50% of sampled commits",
    )
    top_contributor_share: Optional[float] = Field(
        default=None, description="Share of sampled commits by the single top contributor (0..1)"
    )
    issues: IssueMetrics = Field(default_factory=IssueMetrics)


class CommunityHealth(BaseModel):
    """From GitHub's community profile endpoint."""

    health_percentage: Optional[int] = None
    has_readme: bool = False
    has_license: bool = False
    has_contributing: bool = False
    has_code_of_conduct: bool = False
    has_issue_template: bool = False
    has_pull_request_template: bool = False
    has_description: bool = False


class QualitySignals(BaseModel):
    """Heuristics from the repository file tree."""

    has_ci: bool = Field(default=False, description="GitHub Actions workflows present")
    ci_workflows: list[str] = Field(default_factory=list)
    has_tests: bool = Field(default=False, description="Test directories or test files detected")
    has_docs_dir: bool = False
    has_linter_config: bool = False
    linter_configs: list[str] = Field(default_factory=list)
    has_editorconfig: bool = False
    has_precommit_config: bool = False


class SecuritySignals(BaseModel):
    has_security_policy: bool = Field(default=False, description="SECURITY.md present")
    has_dependabot_config: bool = False
    has_codeql_workflow: bool = False
    lockfiles: list[str] = Field(
        default_factory=list, description="Dependency lockfiles found (supply-chain pinning signal)"
    )


class DependencySignals(BaseModel):
    manifests: list[str] = Field(
        default_factory=list, description="Dependency manifest files found in the tree"
    )
    ecosystems: list[str] = Field(
        default_factory=list, description="Package ecosystems inferred from manifests"
    )


class RepoData(BaseModel):
    """All raw facts collected about the repository (the *data* layer)."""

    repo: RepoInfo = Field(default_factory=RepoInfo)
    popularity: Popularity = Field(default_factory=Popularity)
    activity: Activity = Field(default_factory=Activity)
    maintainership: Maintainership = Field(default_factory=Maintainership)
    community: CommunityHealth = Field(default_factory=CommunityHealth)
    quality_signals: QualitySignals = Field(default_factory=QualitySignals)
    security_signals: SecuritySignals = Field(default_factory=SecuritySignals)
    dependencies: DependencySignals = Field(default_factory=DependencySignals)


# ---------------------------------------------------------------------------
# Metrics layer: standardized 1..100 scores
# ---------------------------------------------------------------------------

Band = Literal["critical", "at_risk", "moderate", "good", "excellent"]

ComponentStatus = Literal["met", "partial", "missed", "excluded"]


class MetricComponent(BaseModel):
    """One weighted criterion inside a metric.

    ``status`` summarizes the outcome: ``met`` (full points), ``partial``
    (some points), ``missed`` (zero points), or ``excluded`` (no data or not
    applicable — removed from scoring, weights renormalized).
    """

    name: str
    points: float = Field(description="Points earned (0 when excluded)")
    max_points: float = Field(gt=0, description="Weight of this component within the metric")
    status: ComponentStatus
    detail: Optional[str] = Field(
        default=None, description="The observed value behind the outcome, human-readable"
    )


class Metric(BaseModel):
    """A single standardized metric.

    ``value`` is always an integer in 1..100; ``band`` is the standardized
    interval the value falls into (see docs/metrics.md). ``components`` is the
    per-criterion breakdown the value was computed from; ``inputs`` echoes the
    raw data values, for transparency.
    """

    key: str
    name: str
    value: int = Field(ge=1, le=100)
    band: Band
    components: list[MetricComponent] = Field(
        default_factory=list, description="Per-criterion breakdown of the score"
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict, description="Raw data values this score was computed from"
    )
    note: Optional[str] = Field(
        default=None, description="Caveats, e.g. components skipped due to missing data"
    )


class Metrics(BaseModel):
    """All computed metrics. A metric is None when the underlying data is
    unavailable — missing data is never silently scored."""

    metrics_version: str
    overall: Optional[Metric] = None
    activity: Optional[Metric] = None
    maintainer_resilience: Optional[Metric] = None
    responsiveness: Optional[Metric] = None
    community_health: Optional[Metric] = None
    engineering_practices: Optional[Metric] = None
    security_posture: Optional[Metric] = None


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


class Report(BaseModel):
    """Top-level scanner report: raw data + standardized metrics."""

    schema_version: str = SCHEMA_VERSION
    generated_at: datetime
    source: RepoRef
    data: RepoData = Field(default_factory=RepoData)
    metrics: Optional[Metrics] = None
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal data-collection problems (rate limits, stats not ready, etc.)",
    )
