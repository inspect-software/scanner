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

SCHEMA_VERSION = "0.27.0"

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


class OrgInfo(BaseModel):
    """Public profile of a GitHub organization."""

    login: str
    name: Optional[str] = None
    description: Optional[str] = None
    blog: Optional[str] = None
    location: Optional[str] = None
    email: Optional[str] = None
    twitter_username: Optional[str] = None
    is_verified: bool = Field(
        default=False, description="GitHub verified-domain badge on the organization"
    )
    public_repos: int = 0
    followers: int = 0
    created_at: Optional[datetime] = None
    avatar_url: Optional[str] = None


class OwnerProfile(BaseModel):
    """Public profile of the account owning a repository (user or organization)."""

    login: str
    type: str = Field(description='"User" or "Organization"')
    name: Optional[str] = None
    company: Optional[str] = None
    blog: Optional[str] = None
    location: Optional[str] = None
    followers: int = 0
    public_repos: int = 0
    created_at: Optional[datetime] = None
    account_age_days: Optional[int] = None
    is_verified: Optional[bool] = Field(
        default=None,
        description="GitHub verified-domain badge; only organizations can be verified (None for users)",
    )
    avatar_url: Optional[str] = None


class RepoInfo(BaseModel):
    """Basic repository metadata."""

    owner_type: Optional[str] = Field(
        default=None, description='"User" or "Organization"'
    )
    description: Optional[str] = None
    homepage: Optional[str] = None
    has_wiki: Optional[bool] = None
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
    significant_languages: list[str] = Field(
        default_factory=list,
        description="Languages holding >=10% of total bytes, largest first "
        "(see languages.py) — the languages the repository is written in, "
        "excluding residue like CI scripts and generated markup",
    )
    topics: list[str] = Field(default_factory=list)
    license_spdx: Optional[str] = Field(
        default=None, description="SPDX identifier of the detected license, if any"
    )
    license_spdx_raw: Optional[str] = Field(
        default=None,
        description="Unfiltered spdx_id from GitHub, including the NOASSERTION "
        "sentinel that marks an unrecognized license file",
    )


class StarDay(BaseModel):
    """Stars added on a single calendar day (UTC)."""

    date: str = Field(description="Day the stars were added, ISO ``YYYY-MM-DD`` (UTC)")
    count: int = Field(ge=1, description="Number of stars added that day")


class StarHistory(BaseModel):
    """Per-day star-addition history, for the stars-over-time chart.

    Collected newest-first from GitHub's GraphQL ``stargazers`` connection and
    bucketed by UTC day. Bounded: at most a fixed number of pages is fetched
    (see ``collect.STAR_HISTORY_MAX_PAGES``), so for very popular repositories
    only the most recent window is captured. ``complete`` says whether the
    whole history was reached; when it is False the earliest ``days`` bucket is
    a partial window boundary, and a cumulative curve must be anchored at
    ``total_stars`` (working backwards) rather than at zero.
    """

    total_stars: int = Field(ge=0, description="Repository's current star count")
    collected: int = Field(
        ge=0, description="Star events actually fetched (< total_stars when truncated)"
    )
    complete: bool = Field(
        description="True when pagination reached the end of the stargazers connection "
        "(not merely when collected matches total_stars — GitHub's counter can exceed "
        "the entries the connection lists)"
    )
    days: list[StarDay] = Field(
        default_factory=list, description="Daily star additions, ascending by date"
    )
    # GitHub restricted the stargazers connection to a repository's own admins
    # and collaborators in July 2026, so a history that was collected can never
    # be collected again and is carried forward into later scans instead (see
    # ``collect.scan_repository``). A carried-forward history is frozen at the
    # day it was captured, and every consumer — chart, growth assessment,
    # reader — has to be able to tell how old it is.
    collected_at: Optional[str] = Field(
        default=None,
        description="UTC day the history was captured, ISO ``YYYY-MM-DD``. Older than the "
        "report's own generated_at when the history was carried forward from an earlier scan",
    )


class ForkDay(BaseModel):
    """Forks created on a single calendar day (UTC)."""

    date: str = Field(description="Day the forks were created, ISO ``YYYY-MM-DD`` (UTC)")
    count: int = Field(ge=1, description="Number of forks created that day")


class ForkHistory(BaseModel):
    """Per-day fork-creation history, for the forks-over-time chart.

    Collected newest-first from GitHub's GraphQL ``forks`` connection (each fork
    node's ``createdAt`` is when the fork — and so the fork event — was created)
    and bucketed by UTC day. Bounded exactly like ``StarHistory`` (see
    ``collect.FORK_HISTORY_MAX_PAGES``); ``complete`` says whether the whole
    history was reached, and when False a cumulative curve must be anchored at
    ``total_forks`` working backwards rather than at zero.
    """

    total_forks: int = Field(ge=0, description="Repository's current fork count")
    collected: int = Field(
        ge=0, description="Fork events actually fetched (< total_forks when truncated)"
    )
    complete: bool = Field(
        description="True when pagination reached the end of the forks connection "
        "(not merely when collected matches total_forks — forkCount can exceed "
        "the forks the connection lists)"
    )
    days: list[ForkDay] = Field(
        default_factory=list, description="Daily fork additions, ascending by date"
    )


class Popularity(BaseModel):
    stars: int = 0
    forks: int = 0
    watchers: int = Field(default=0, description="Subscribers (users watching for notifications)")
    open_issues_and_prs: int = Field(
        default=0, description="GitHub's combined open issues + open PRs counter"
    )
    star_history: Optional[StarHistory] = Field(
        default=None,
        description="Per-day star additions for the stars-over-time chart; None when "
        "unavailable (no token, or the GraphQL fetch failed)",
    )
    fork_history: Optional[ForkHistory] = Field(
        default=None,
        description="Per-day fork additions for the forks-over-time chart; None when "
        "unavailable (no token, or the GraphQL fetch failed)",
    )


class ContributorOrganization(BaseModel):
    """A public GitHub organization membership declared on a contributor profile."""

    login: str
    name: Optional[str] = None
    location: Optional[str] = None


class ContributorProfile(BaseModel):
    """Optional public profile enrichment for a displayed top contributor."""

    name: Optional[str] = None
    location: Optional[str] = None
    company: Optional[str] = None
    organizations: list[ContributorOrganization] = Field(default_factory=list)


class Contributor(BaseModel):
    login: str
    commits: int
    type: Optional[str] = None
    avatar_url: Optional[str] = None
    profile: Optional[ContributorProfile] = None


class CommitRecord(BaseModel):
    """One commit from the default branch, as written by its author.

    The message is kept split the way GitHub returns it: ``headline`` is the
    first line (the subject), ``body`` everything after it. A long body is
    shortened from the middle rather than the end (see
    ``collect._truncate_body``), because git trailers — the ``Co-authored-by``
    and ``Generated with`` lines that identify bots and coding agents — sit at
    the very end of a message.
    """

    oid: str = Field(description="Full commit SHA")
    committed_at: datetime = Field(description="Committer date")
    headline: str = Field(description="First line of the commit message")
    body: Optional[str] = Field(
        default=None,
        description="Message after the first line; None when there is none. When "
        "truncated, head and tail are kept with an elision marker between them",
    )
    body_truncated: bool = Field(
        default=False, description="True when the middle of body was elided"
    )
    author_login: Optional[str] = Field(
        default=None, description="GitHub login of the author; None for unlinked authors"
    )
    author_name: Optional[str] = Field(
        default=None, description="Author name from the git commit itself"
    )
    is_bot: bool = Field(
        default=False,
        description="Authoring account is an automation account (a GitHub App: "
        "Dependabot, Renovate, a CI bot), not a person",
    )
    is_coding_agent: bool = Field(
        default=False,
        description="An LLM coding agent wrote the change — either committing "
        "under its own account or credited in a Co-authored-by trailer. "
        "Independent of is_bot: a human commit produced with an agent has "
        "is_bot=false and is_coding_agent=true. See bots.py for the detection "
        "keys and their known incompleteness",
    )


class ReleaseRecord(BaseModel):
    """One release, with its version tag classified by semantic-version level.

    ``kind`` reads the tag alone rather than diffing consecutive versions, so a
    gap in the fetched window cannot mislabel a release: ``X.0.0`` is major,
    ``X.Y.0`` minor, anything else with a three-part version is a patch, a
    ``-rc``/``-beta`` suffix makes it a prerelease, and a tag that is not
    semver at all is ``other``.
    """

    tag: str
    published_at: Optional[datetime] = Field(
        default=None, description="Publication date; None when the tag carries no date"
    )
    kind: Literal["major", "minor", "patch", "prerelease", "other"] = Field(
        description="Semantic-version level of the tag"
    )


class Activity(BaseModel):
    """Commit / release activity facts."""

    recent_commits: list[CommitRecord] = Field(
        default_factory=list,
        description="Up to 100 newest commits on the default branch, newest first. "
        "Empty when unavailable (no token, empty repository, or the GraphQL "
        "snapshot fell back to REST)",
    )
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
    releases_from_tags: bool = Field(
        default=False,
        description=(
            "True when release facts were derived from semver git tags because the "
            "repo publishes no GitHub Releases"
        ),
    )
    releases: list[ReleaseRecord] = Field(
        default_factory=list,
        description="Up to 100 most recent releases, newest first, for the release "
        "timeline. Carried by the same snapshot the aggregates above are derived "
        "from, so it costs no extra request",
    )
    latest_release_tag: Optional[str] = None
    latest_release_at: Optional[datetime] = None
    days_since_latest_release: Optional[int] = None
    mean_days_between_releases: Optional[float] = Field(
        default=None, description="Mean gap between the most recent releases (up to 10)"
    )


class TrackedItem(BaseModel):
    """One open issue or pull request, with whatever last happened on it.

    Sampled oldest-first: the question these answer is not what the tracker is
    busy with, it is what has been sitting in it unanswered — and that is at
    the far end of the queue, not the near one.
    """

    number: int
    created_at: datetime
    last_comment_at: Optional[datetime] = Field(
        default=None, description="Most recent comment; None when nobody has replied at all"
    )
    last_comment_author: Optional[str] = Field(
        default=None,
        description="Login of the most recent commenter — read to tell a maintainer "
        "reply from the author talking to themselves; None when there is no comment",
    )


class ContributionFlow(BaseModel):
    """Whether work arriving from outside is still being acted on.

    Separate from ``Activity``, which measures what maintainers *emit*. This
    measures what they *answer*, and the difference is the whole point: a
    finished library emits nothing and owes nothing, while an abandoned one
    emits nothing while requests pile up against it.

    Carried by the same GraphQL snapshot as everything else, so it costs no
    additional request. ``collected`` is False on the REST fallback path and
    on unauthenticated scans, where every field below stays empty and nothing
    derived from them may be scored.
    """

    collected: bool = Field(
        default=False, description="The GraphQL snapshot supplied these fields"
    )
    last_merged_pr_at: Optional[datetime] = Field(
        default=None,
        description="Merge date of the most recently merged pull request seen in the "
        "sample; None when the repository has never merged one",
    )
    oldest_open_prs: list[TrackedItem] = Field(
        default_factory=list, description="Up to 20 longest-open pull requests, oldest first"
    )
    oldest_open_issues: list[TrackedItem] = Field(
        default_factory=list, description="Up to 20 longest-open issues, oldest first"
    )
    ci_last_run_at: Optional[datetime] = Field(
        default=None,
        description="When CI last reported on the default branch head; None when the "
        "repository runs no checks",
    )
    ci_last_conclusion: Optional[str] = Field(
        default=None,
        description="Conclusion of that run (SUCCESS, FAILURE, …), verbatim from GitHub",
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
    """Who maintains the project, and how concentrated that work is.

    Every figure here counts people only. Automation accounts are removed
    before anything is derived — a bus factor computed over a list where
    Renovate and Dependabot rank second and third (measured: prettier, vuejs/core)
    describes the release robots, not the maintainers. ``bot_contributors``
    keeps the removal visible rather than silent.
    """

    contributors_sampled: Optional[int] = Field(
        default=None,
        description="Human contributors counted (capped at 100 by the API page size)",
    )
    bot_contributors: Optional[int] = Field(
        default=None,
        description="Automation accounts excluded from every figure in this "
        "section. Undercounts: bots running as ordinary user accounts "
        "(kubernetes' k8s-ci-robot) are indistinguishable from people here",
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


class ScorecardCheck(BaseModel):
    """One OpenSSF Scorecard check result.

    ``score`` is 0..10, or ``None`` when Scorecard returned ``-1`` — meaning
    *inconclusive* (it could not determine the answer, e.g. Branch-Protection
    without an admin token). Inconclusive checks carry no information and are
    excluded from scoring (weights renormalized), never counted as zero.
    """

    name: str = Field(description="Scorecard check name, e.g. 'Token-Permissions'")
    score: Optional[int] = Field(
        default=None, ge=0, le=10, description="0..10; None when Scorecard reported -1 (inconclusive)"
    )
    reason: Optional[str] = None
    documentation_url: Optional[str] = None


class Scorecard(BaseModel):
    """OpenSSF Scorecard result for the repository, produced by the open-source
    ``scorecard`` CLI (https://github.com/ossf/scorecard).

    Scorecard is a neutral, versioned security-scoring standard — its checks
    are tool-agnostic (any accepted SAST/dependency-update/etc. tool earns
    credit), which is exactly why we lean on it instead of detecting specific
    vendor config files. ``aggregate_score`` is Scorecard's own 0..10 headline
    number; ``checks`` are the per-check breakdown."""

    aggregate_score: Optional[float] = Field(
        default=None, description="Scorecard's headline score, 0..10 (None if it could not compute)"
    )
    checks: list[ScorecardCheck] = Field(default_factory=list)
    scorecard_version: Optional[str] = None
    ran_at: Optional[datetime] = None
    commit: Optional[str] = Field(default=None, description="Repo commit Scorecard evaluated")


class SecuritySignals(BaseModel):
    has_security_policy: bool = Field(default=False, description="SECURITY.md present")
    has_dependabot_config: bool = False
    has_codeql_workflow: bool = False
    lockfiles: list[str] = Field(
        default_factory=list, description="Dependency lockfiles found (supply-chain pinning signal)"
    )
    scorecard: Optional[Scorecard] = Field(
        default=None,
        description="OpenSSF Scorecard result, when the scorecard CLI was available and ran",
    )


class AIReadinessSignals(BaseModel):
    """Heuristics for how well the repo is equipped to be developed and
    maintained with AI coding agents. Collected from the file tree (paths and
    blob sizes) — see docs/metrics.md, "AI Readiness". Presence-based and
    coarse: signals that the infrastructure exists, not how good it is.

    This is an *independent, additive* signal — the AI Readiness category
    carries weight 0.0 in the overall score, so it is surfaced as its own badge
    and never drags a solid pre-AI-era project's health score down.
    """

    agent_instruction_files: list[str] = Field(
        default_factory=list,
        description="Agent guidance files found (CLAUDE.md, AGENTS.md, .cursor/rules, "
        ".github/copilot-instructions.md, GEMINI.md, .windsurfrules, ...)",
    )
    agent_instruction_max_bytes: Optional[int] = Field(
        default=None, description="Size of the largest agent instruction file (stub detection)"
    )
    has_llms_txt: bool = Field(
        default=False, description="llms.txt / llms-full.txt machine-readable docs entrypoint"
    )
    bootstrap_files: list[str] = Field(
        default_factory=list,
        description="One-command bootstrap / task runners (Makefile, Taskfile, justfile, ...)",
    )
    toolchain_manifests: list[str] = Field(
        default_factory=list,
        description="Manifests whose toolchain defines the build/test command itself "
        "(Cargo.toml -> `cargo test`, go.mod -> `go test`, ...). A weaker bootstrap "
        "signal than a task runner, but a real one: these ecosystems need no Makefile",
    )
    typecheck_configs: list[str] = Field(
        default_factory=list,
        description="Static type-check configs found (mypy.ini, pyrightconfig.json, tsconfig.json, py.typed, ...)",
    )
    has_devcontainer: bool = False
    has_dockerfile: bool = False
    has_nix: bool = Field(default=False, description="Nix flake / shell.nix / default.nix present")
    api_schema_files: list[str] = Field(
        default_factory=list,
        description="Machine-readable interface schemas (OpenAPI/Swagger, GraphQL, protobuf, AsyncAPI)",
    )
    has_mcp_signal: bool = Field(
        default=False, description="Model Context Protocol server signal (dependency or mcp config)"
    )
    example_dirs: list[str] = Field(
        default_factory=list, description="Directories of runnable examples/recipes, and notebooks"
    )
    source_files_sampled: int = Field(
        default=0, description="Non-vendored source files considered for the file-size signal"
    )
    oversized_source_files: int = Field(
        default=0, description="Source files above the agent-legibility size threshold"
    )
    largest_source_bytes: Optional[int] = None


class Dependency(BaseModel):
    """One dependency declared directly in a manifest file, parsed from its
    own text — not resolved against a registry. Reported exactly as declared;
    no freshness or vulnerability checks are performed (see the "Not yet
    integrated" note in docs/ecosystems.md)."""

    ecosystem: str = Field(
        description="pypi | npm | packagist | crates | go | maven | rubygems | nuget | hex"
    )
    name: str
    version_constraint: Optional[str] = Field(
        default=None, description="As declared in the manifest, verbatim (e.g. \"^3.1.50\")"
    )
    manifest: str = Field(description="Manifest file path this dependency was declared in")


class ResolvedDependency(BaseModel):
    """One package in the resolved dependency set (direct + transitive),
    as reported by GitHub's dependency-graph SBOM export."""

    ecosystem: str = Field(
        description="pypi | npm | packagist | crates | go | maven | rubygems | nuget | hex"
    )
    name: str
    version: Optional[str] = Field(
        default=None, description="Resolved version when the graph knows it (lockfile-backed)"
    )
    direct: bool = Field(
        description="Matches a declared direct runtime dependency; False = indirect/transitive "
        "(or a direct dev/test dependency, which the declared list excludes)"
    )


class AllDependencies(BaseModel):
    """The full resolved dependency set — direct plus indirect/transitive —
    from GitHub's dependency-graph SBOM export.

    Collection is strictly best-effort and time-boxed: when it fails or the
    budget runs out, ``collected`` is False, ``error`` says why (mirrored in
    the report warnings), and the scan continues unaffected. The declared
    direct list (``DependencySignals.dependencies``) is collected
    independently and is never impacted."""

    collected: bool = Field(
        default=False, description="The resolved graph was retrieved successfully"
    )
    source: Optional[str] = Field(
        default=None, description='Where the graph came from ("github-sbom"); None if not collected'
    )
    error: Optional[str] = Field(
        default=None, description="Why the graph could not be collected (also a report warning)"
    )
    total_count: Optional[int] = Field(
        default=None, description="Resolved packages in the graph (always complete, even when the list is truncated)"
    )
    direct_count: Optional[int] = None
    indirect_count: Optional[int] = None
    truncated: bool = Field(
        default=False,
        description="The embedded package list was capped to keep reports bounded; counts remain complete",
    )
    packages: list[ResolvedDependency] = Field(
        default_factory=list, description="Resolved packages, direct entries first"
    )


class AdvisoryFinding(BaseModel):
    """One resolved dependency carrying known advisories.

    ``severity`` is the worst severity across the package's advisories, from
    the advisory database's own label; "unknown" where the record carries none
    (common for PYSEC entries) rather than a guess."""

    ecosystem: str
    name: str
    version: Optional[str] = None
    direct: bool = Field(
        description="Matches a declared direct runtime dependency; False = indirect, "
        "or a direct dev/test dependency, which the declared list excludes"
    )
    severity: str = Field(description="critical | high | moderate | low | unknown")
    cvss_score: Optional[float] = Field(
        default=None,
        description="Highest CVSS base score across this package's advisories, computed "
        "from the published vector; null where no advisory carries one",
    )
    oldest_advisory_days: Optional[int] = Field(
        default=None,
        description="Days since the earliest of this package's advisories was published — "
        "how long a fix has been available and unapplied",
    )
    advisory_count: int = Field(description="Distinct advisories affecting this version")
    advisory_ids: list[str] = Field(
        default_factory=list, description="Advisory identifiers (OSV/GHSA/PYSEC), capped at 10"
    )
    fixed_version: Optional[str] = Field(
        default=None, description="Highest version an advisory records as fixed, when stated"
    )


class MaliciousDependency(BaseModel):
    """One resolved dependency OSV reports as a malicious package.

    Sourced from the OpenSSF ``ossf/malicious-packages`` corpus, which OSV.dev
    serves under ``MAL-`` identifiers, so it arrives on the same batch query as
    ordinary advisories at no extra cost.

    This is not a vulnerability finding and carries none of a vulnerability's
    fields: malware has no CVSS vector and no severity band. The remedy is
    removal, or moving off the compromised name — never an upgrade to a fixed
    release of the same artifact, because there is none."""

    ecosystem: str
    name: str
    version: Optional[str] = Field(
        default=None, description="Version resolved in the dependency graph"
    )
    direct: bool = Field(
        description="Matches a declared direct runtime dependency; False = indirect. "
        "Both are scored alike — an install-time payload runs at any depth."
    )
    advisory_ids: list[str] = Field(
        default_factory=list, description="Malicious-package report identifiers, capped at 10"
    )
    first_reported_at: Optional[datetime] = Field(
        default=None,
        description="Earliest publication date across the reports, when the record states one",
    )
    still_published: Optional[bool] = Field(
        default=None,
        description="Whether the registry still serves this exact version. False means the "
        "registry removed it, so the reported artifact can no longer be installed and the "
        "finding is reported without being scored. None means the check did not run or the "
        "ecosystem is not covered — treated as still published, the conservative direction.",
    )


class DependencyAdvisories(BaseModel):
    """Known advisories affecting the resolved dependency set, from OSV.dev.

    Best-effort like the dependency graph it reads from: on any failure
    ``collected`` is False and ``error`` says why, and the advisory metric is
    excluded from scoring rather than counted as zero.

    An entry here means the version recorded in the dependency graph falls in
    an advisory's affected range. It is not a reachability or exploitability
    finding, and the graph includes development and test pins that GitHub's
    export does not distinguish from runtime dependencies."""

    collected: bool = Field(default=False, description="The advisory lookup completed")
    source: Optional[str] = Field(default=None, description='Advisory source ("osv")')
    scope: Optional[str] = Field(
        default=None,
        description='What was assessed: "published_package" (the runtime closure of the '
        "published package, from deps.dev — what installing it pulls in) or "
        '"repository_graph" (the repository dependency graph, which also contains '
        "development and test pins)",
    )
    assessed_package: Optional[str] = Field(
        default=None,
        description='The published package assessed, as "ecosystem:name@version"; '
        "null in repository_graph scope",
    )
    error: Optional[str] = Field(
        default=None, description="Why advisories could not be collected (also a report warning)"
    )
    assessed_count: int = Field(
        default=0, description="Resolved packages actually queried (version and ecosystem known)"
    )
    unassessed_count: int = Field(
        default=0, description="Resolved packages skipped — no version, or unsupported ecosystem"
    )
    affected_count: int = Field(default=0, description="Assessed packages carrying advisories")
    direct_affected_count: int = Field(
        default=0, description="Affected packages that are declared direct runtime dependencies"
    )
    advisory_count: int = Field(
        default=0, description="Total advisories across affected packages"
    )
    by_severity: dict[str, int] = Field(
        default_factory=dict, description="Affected package counts keyed by worst severity"
    )
    truncated: bool = Field(
        default=False, description="The embedded findings list was capped; counts remain complete"
    )
    findings: list[AdvisoryFinding] = Field(
        default_factory=list, description="Affected packages, most severe first"
    )
    malicious_count: int = Field(
        default=0, description="Assessed packages reported as malicious packages"
    )
    malicious: list[MaliciousDependency] = Field(
        default_factory=list,
        description="Dependencies reported as malicious packages, direct entries first. "
        "Excluded from findings and from every advisory count above — malware is "
        "reported and scored as its own finding, not as an advisory",
    )


class DependencySignals(BaseModel):
    manifests: list[str] = Field(
        default_factory=list, description="Dependency manifest files found in the tree"
    )
    ecosystems: list[str] = Field(
        default_factory=list, description="Package ecosystems inferred from manifests"
    )
    dependencies: list[Dependency] = Field(
        default_factory=list,
        description="Direct runtime dependencies declared in supported manifests "
        "(pyproject.toml, setup.cfg, package.json, composer.json, Cargo.toml, "
        "go.mod, pom.xml, Gemfile, *.csproj, mix.exs); dev/test dependency "
        "groups and platform pseudo-packages are excluded",
    )
    all_dependencies: AllDependencies = Field(
        default_factory=AllDependencies,
        description="Full resolved dependency set (direct + transitive) from the "
        "GitHub dependency-graph SBOM; best-effort, see AllDependencies",
    )
    advisories: DependencyAdvisories = Field(
        default_factory=DependencyAdvisories,
        description="Known advisories affecting the resolved set, matched against "
        "OSV.dev; best-effort, see DependencyAdvisories",
    )


class EcosystemPackage(BaseModel):
    """Facts about a published package this repository ships, from a package
    registry (PyPI, npm, Packagist, crates.io, the Go module proxy, Maven
    Central, NuGet, …). Registry data is richer and more adoption-relevant
    than GitHub stars: real download counts, publish cadence, and
    deprecation/yank flags."""

    ecosystem: str = Field(
        description="pypi | npm | packagist | crates | rubygems | hex | go | maven | nuget"
    )
    name: str = Field(description="Package identifier within the ecosystem")
    registry_url: str
    exists: bool = Field(default=True, description="Package was found on the registry")
    matches_repo: Optional[bool] = Field(
        default=None,
        description="Registry's repository URL points back to the scanned repo "
        "(None when the registry declares no repository)",
    )

    latest_version: Optional[str] = None
    latest_published_at: Optional[datetime] = None
    days_since_latest_publish: Optional[int] = None
    first_published_at: Optional[datetime] = None
    versions_count: Optional[int] = None

    monthly_downloads: Optional[int] = Field(
        default=None, description="Downloads in the last ~30 days (approximated for crates.io)"
    )
    total_downloads: Optional[int] = None
    dependents_count: Optional[int] = Field(
        default=None, description="Number of registry packages depending on this one, if published"
    )

    license: Optional[str] = None
    maintainers_count: Optional[int] = None
    is_deprecated: bool = Field(
        default=False, description="npm deprecated / Packagist abandoned"
    )
    deprecation_note: Optional[str] = Field(
        default=None, description="Deprecation message or suggested replacement"
    )
    latest_version_yanked: Optional[bool] = None
    repository_url: Optional[str] = Field(
        default=None, description="Repository URL the registry declares for the package"
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Tags/keywords/categories/classifiers the registry lists for the "
        "package (PyPI classifiers, npm keywords, crates.io categories+keywords, "
        "Packagist keywords, NuGet tags, …); empty where the registry has no such "
        "concept or none were declared",
    )


class EcosystemData(BaseModel):
    """Registry facts for every package the repository publishes."""

    packages: list[EcosystemPackage] = Field(default_factory=list)


ContactKind = Literal["email", "url", "handle"]

ContactRole = Literal[
    "owner", "author", "maintainer", "security", "funding", "issues", "chat", "support"
]


class ContactChannel(BaseModel):
    """One way to reach the people responsible for a project.

    Every value is self-published by the maintainer on a public profile or
    registry — but it is still personal data, and it is not part of the public
    report: ``data.contacts`` is stripped from the report endpoint (see
    ``PUBLIC_REPORT_EXCLUDE`` in the website backend) and is never rendered.
    Nothing here is scored; contacts exist to reach maintainers, not to judge
    them.
    """

    kind: ContactKind
    value: str = Field(description="Address, URL, or @handle, verbatim as published")
    role: ContactRole = Field(
        description="What the channel is for, as declared by its source field or label"
    )
    source: str = Field(
        description='Where it was read from: "github-owner-profile", "pypi", "npm", '
        '"rubygems", "hex", "packagist", "crates", or a SECURITY.md path'
    )


LicenseState = Literal["standard", "custom", "absent"]


class LicenseInfo(BaseModel):
    """The repository's license, resolved from every source at once.

    ``state`` is the single fact consumers should read. ``standard`` means
    GitHub recognized a specific license; ``custom`` means a license file
    exists but its text is not a recognized license (GitHub's ``NOASSERTION``);
    ``absent`` means no source found a file at all. See ``license.py`` for the
    resolution rules — nothing else may re-derive this.
    """

    state: LicenseState = "absent"
    spdx_id: Optional[str] = Field(
        default=None, description="SPDX identifier, set only when state is 'standard'"
    )
    raw_spdx: Optional[str] = Field(
        default=None, description="What GitHub reported, including the NOASSERTION sentinel"
    )
    file_present: bool = Field(
        default=False, description="Any source found a license file (logical OR)"
    )
    profile_has_license: bool = Field(
        default=False, description="GitHub community profile saw a license file"
    )
    scorecard_found: Optional[bool] = Field(
        default=None, description="Scorecard's License check found one; None when no Scorecard ran"
    )


class RepoData(BaseModel):
    """All raw facts collected about the repository (the *data* layer)."""

    contacts: list[ContactChannel] = Field(
        default_factory=list,
        description="Maintainer contact channels. Personal data — excluded from "
        "the public report endpoint and never rendered or scored.",
    )
    owner: Optional[OwnerProfile] = Field(
        default=None,
        description="Public profile of the owning account (organization or user)",
    )
    repo: RepoInfo = Field(default_factory=RepoInfo)
    popularity: Popularity = Field(default_factory=Popularity)
    activity: Activity = Field(default_factory=Activity)
    contribution_flow: ContributionFlow = Field(default_factory=ContributionFlow)
    maintainership: Maintainership = Field(default_factory=Maintainership)
    community: CommunityHealth = Field(default_factory=CommunityHealth)
    license: LicenseInfo = Field(default_factory=LicenseInfo)
    quality_signals: QualitySignals = Field(default_factory=QualitySignals)
    security_signals: SecuritySignals = Field(default_factory=SecuritySignals)
    dependencies: DependencySignals = Field(default_factory=DependencySignals)
    ai_readiness: AIReadinessSignals = Field(default_factory=AIReadinessSignals)
    ecosystem: EcosystemData = Field(default_factory=EcosystemData)


# ---------------------------------------------------------------------------
# Metrics layer: standardized 1..100 scores
# ---------------------------------------------------------------------------

Band = Literal["critical", "at_risk", "moderate", "good", "excellent"]

ComponentStatus = Literal["met", "partial", "missed", "excluded"]


class MetricNote(BaseModel):
    """One machine-identified statement attached to a metric.

    A metric's ``note`` is generated English prose. Localized surfaces cannot
    translate generated text, so the same statements are also reported as a
    code plus its parameters, and a reader's language is applied at render
    time. ``note`` remains authoritative for consumers that read prose.
    """

    code: str = Field(description="Stable identifier of the statement, e.g. 'weights_renormalized'")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Values the statement is about (counts, names, keys)"
    )


class MetricComponent(BaseModel):
    """One weighted criterion inside a metric.

    ``status`` summarizes the outcome: ``met`` (full points), ``partial``
    (some points), ``missed`` (zero points), or ``excluded`` (no data or not
    applicable — removed from scoring, weights renormalized).
    """

    key: str = Field(
        default="",
        description="Stable slug of the criterion, derived from its name — the handle a "
        "localized surface renders a translated label from",
    )
    name: str
    points: float = Field(description="Points earned (0 when excluded)")
    max_points: float = Field(gt=0, description="Weight of this component within the metric")
    status: ComponentStatus
    detail: Optional[str] = Field(
        default=None, description="The observed value behind the outcome, human-readable"
    )
    details: list[MetricNote] = Field(
        default_factory=list,
        description="The same observation as ``detail``, machine-identified so a "
        "localized surface can state it in the reader's language. Empty where the "
        "text came from outside the scanner (an OpenSSF Scorecard reason, a "
        "registry's deprecation note, a URL) and is not ours to translate",
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
    notes: list[MetricNote] = Field(
        default_factory=list,
        description="The statements in ``note``, machine-identified so a localized "
        "surface can render them in the reader's language",
    )
    inputs: dict[str, Any] = Field(
        default_factory=dict, description="Raw data values this score was computed from"
    )
    note: Optional[str] = Field(
        default=None, description="Caveats, e.g. components skipped due to missing data"
    )


class MetricCategory(BaseModel):
    """A group of related metrics with its own rolled-up score.

    ``value`` is the weighted mean (1..100) of the category's available
    metrics; ``None`` when no metric in the category could be scored.
    """

    key: str
    name: str
    description: str
    weight: float = Field(description="Weight of this category in the overall score")
    value: Optional[int] = Field(default=None, ge=1, le=100)
    band: Optional[Band] = None
    metrics: list[Metric] = Field(default_factory=list)


class Metrics(BaseModel):
    """All computed metrics, grouped into categories.

    A metric or category is None/absent when the underlying data is
    unavailable — missing data is never silently scored."""

    metrics_version: str
    overall: Optional[Metric] = None
    categories: list[MetricCategory] = Field(default_factory=list)

    def by_key(self, key: str) -> Optional[Metric]:
        """Look up a single metric across all categories by its key."""
        for category in self.categories:
            for metric in category.metrics:
                if metric.key == key:
                    return metric
        return None

    def category(self, key: str) -> Optional[MetricCategory]:
        for category in self.categories:
            if category.key == key:
                return category
        return None


# ---------------------------------------------------------------------------
# Organization report
# ---------------------------------------------------------------------------


class OrgRef(BaseModel):
    """Identity of the scanned organization."""

    url: str
    host: str = "github.com"
    login: str


class TopRepo(BaseModel):
    name: str
    stars: int = 0
    pushed_at: Optional[datetime] = None
    description: Optional[str] = None


class OrgPortfolio(BaseModel):
    """Aggregate facts about the organization's public repositories.

    Computed over a sample of up to 100 public repos (API page cap),
    most recently pushed first.
    """

    repos_sampled: int = 0
    total_stars_sampled: int = 0
    repos_pushed_90d: Optional[int] = None
    repos_pushed_365d: Optional[int] = None
    original_repos_sampled: int = Field(default=0, description="Non-fork repos in the sample")
    forks_sampled: int = 0
    top_repos: list[TopRepo] = Field(
        default_factory=list, description="Up to 5 sampled repos with the most stars"
    )
    public_members: Optional[int] = Field(
        default=None, description="Publicly visible members (capped at 100)"
    )


class OrgData(BaseModel):
    """All raw facts collected about the organization (the *data* layer)."""

    info: OrgInfo
    portfolio: OrgPortfolio = Field(default_factory=OrgPortfolio)


class OrgMetrics(BaseModel):
    """Standardized 1..100 scores for an organization, grouped into categories."""

    metrics_version: str
    overall: Optional[Metric] = None
    categories: list[MetricCategory] = Field(default_factory=list)

    def by_key(self, key: str) -> Optional[Metric]:
        for category in self.categories:
            for metric in category.metrics:
                if metric.key == key:
                    return metric
        return None


# ---------------------------------------------------------------------------
# Scan configuration
# ---------------------------------------------------------------------------


class ScanConfig(BaseModel):
    """Which parts of the scoring methodology are active for a scan.

    Scoring is a three-level hierarchy — ``components → metrics → categories``
    — and any level can be switched off. Disabling something removes it from
    scoring *exactly as if its data were unavailable*: the remaining weights
    are renormalized so the score stays on the 1..100 scale. Disabling is
    therefore transparent rather than a silent zero.

    Every report embeds the ``ScanConfig`` that produced it, so a score is
    reproducible and it is explicit what was and was not measured. Empty
    collections mean "everything enabled" — the default full methodology.
    """

    disabled_categories: list[str] = Field(
        default_factory=list, description="Category keys excluded from scoring"
    )
    disabled_metrics: list[str] = Field(
        default_factory=list, description="Metric keys excluded from scoring"
    )
    disabled_components: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Metric key -> component names excluded within that metric",
    )

    def category_enabled(self, key: str) -> bool:
        return key not in self.disabled_categories

    def metric_enabled(self, key: str) -> bool:
        return key not in self.disabled_metrics

    def disabled_component_names(self, metric_key: str) -> set[str]:
        return set(self.disabled_components.get(metric_key, ()))

    @property
    def is_default(self) -> bool:
        """True when nothing is disabled (the full methodology)."""
        return not (
            self.disabled_categories or self.disabled_metrics or self.disabled_components
        )


# ---------------------------------------------------------------------------
# Top-level reports
# ---------------------------------------------------------------------------


class Report(BaseModel):
    """Top-level repository report: raw data + standardized metrics."""

    report_type: Literal["repository"] = "repository"
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime
    source: RepoRef
    config: ScanConfig = Field(
        default_factory=ScanConfig,
        description="The scan configuration that produced this report's metrics",
    )
    data: RepoData = Field(default_factory=RepoData)
    metrics: Optional[Metrics] = None
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal data-collection problems (rate limits, stats not ready, etc.)",
    )


class OrgReport(BaseModel):
    """Top-level organization report: raw data + standardized metrics."""

    report_type: Literal["organization"] = "organization"
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime
    source: OrgRef
    config: ScanConfig = Field(
        default_factory=ScanConfig,
        description="The scan configuration that produced this report's metrics",
    )
    data: OrgData
    metrics: Optional[OrgMetrics] = None
    warnings: list[str] = Field(default_factory=list)
