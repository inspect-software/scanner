"""Collect public GitHub data for a repository and assemble a Report."""

from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from typing import Any, Optional

from .github import GitHubClient, GitHubError, RepoNotFoundError, parse_repo_url
from .metrics import compute_metrics, compute_org_metrics
from .models import (
    Activity,
    CommunityHealth,
    Contributor,
    DependencySignals,
    IssueMetrics,
    Maintainership,
    OrgData,
    OrgInfo,
    OrgPortfolio,
    OrgRef,
    OrgReport,
    Popularity,
    QualitySignals,
    RepoData,
    Report,
    RepoInfo,
    RepoRef,
    SecuritySignals,
    TopRepo,
)

TOP_CONTRIBUTORS_SHOWN = 10
RELEASES_FOR_CADENCE = 10

# Manifest filename (glob) -> ecosystem name.
MANIFEST_ECOSYSTEMS: dict[str, str] = {
    "pyproject.toml": "pypi",
    "setup.py": "pypi",
    "setup.cfg": "pypi",
    "requirements*.txt": "pypi",
    "Pipfile": "pypi",
    "package.json": "npm",
    "composer.json": "packagist",
    "Cargo.toml": "crates",
    "go.mod": "go",
    "Gemfile": "rubygems",
    "pom.xml": "maven",
    "build.gradle": "maven",
    "build.gradle.kts": "maven",
    "*.csproj": "nuget",
    "mix.exs": "hex",
}

LOCKFILE_NAMES = {
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "pdm.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    "packages.lock.json",
    "mix.lock",
}

LINTER_CONFIG_NAMES = {
    ".flake8",
    ".pylintrc",
    "ruff.toml",
    ".ruff.toml",
    "tox.ini",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    "eslint.config.js",
    "eslint.config.mjs",
    "biome.json",
    ".rubocop.yml",
    ".golangci.yml",
    ".golangci.yaml",
    "phpcs.xml",
    "phpstan.neon",
    ".php-cs-fixer.php",
    ".php-cs-fixer.dist.php",
}

TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__", "testing"}
TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "*_test.go", "*.test.js", "*.test.ts", "*.spec.js", "*.spec.ts", "*Test.java", "*Test.php")


def scan_repository(url: str, token: Optional[str] = None) -> Report:
    """Scan a public GitHub repository and return a populated Report."""
    owner, name = parse_repo_url(url)
    source = RepoRef(url=url, owner=owner, name=name)
    warnings: list[str] = []

    with GitHubClient(token=token) as gh:
        base = f"/repos/{owner}/{name}"
        try:
            repo_data = gh.get(base)
        except RepoNotFoundError:
            raise RepoNotFoundError(
                f"Repository {owner}/{name} not found or not publicly accessible"
            ) from None

        data = RepoData(
            repo=_repo_info(gh, base, repo_data, warnings),
            popularity=_popularity(repo_data),
            activity=_activity(gh, base, repo_data, warnings),
            maintainership=_maintainership(gh, base, owner, name, warnings),
            community=_community(gh, base, warnings),
        )
        if data.repo.owner_type == "Organization":
            org_raw = gh.get_optional(f"/orgs/{owner}")
            if org_raw:
                data.owner_org = _org_info(org_raw)
            else:
                warnings.append("Owning organization profile unavailable")

        tree_paths = _fetch_tree(gh, base, repo_data.get("default_branch"), warnings)
        data.quality_signals = _quality(tree_paths)
        data.security_signals = _security(tree_paths, data.community)
        data.dependencies = _dependencies(tree_paths)

        return Report(
            generated_at=datetime.now(timezone.utc),
            source=source,
            data=data,
            metrics=compute_metrics(data),
            warnings=warnings,
        )


def _repo_info(
    gh: GitHubClient, base: str, data: dict[str, Any], warnings: list[str]
) -> RepoInfo:
    languages = gh.get_optional(f"{base}/languages") or {}
    if not languages:
        warnings.append("Language breakdown unavailable")
    license_info = data.get("license") or {}
    return RepoInfo(
        owner_type=(data.get("owner") or {}).get("type"),
        description=data.get("description"),
        homepage=data.get("homepage") or None,
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        pushed_at=data.get("pushed_at"),
        default_branch=data.get("default_branch"),
        is_fork=data.get("fork", False),
        is_archived=data.get("archived", False),
        is_disabled=data.get("disabled", False),
        size_kb=data.get("size"),
        primary_language=data.get("language"),
        languages=languages,
        topics=data.get("topics") or [],
        license_spdx=license_info.get("spdx_id")
        if license_info.get("spdx_id") not in (None, "NOASSERTION")
        else None,
    )


def _popularity(data: dict[str, Any]) -> Popularity:
    return Popularity(
        stars=data.get("stargazers_count", 0),
        forks=data.get("forks_count", 0),
        watchers=data.get("subscribers_count", 0),
        open_issues_and_prs=data.get("open_issues_count", 0),
    )


def _activity(
    gh: GitHubClient, base: str, repo_data: dict[str, Any], warnings: list[str]
) -> Activity:
    activity = Activity()

    pushed_at = repo_data.get("pushed_at")
    if pushed_at:
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        activity.days_since_last_push = (datetime.now(timezone.utc) - pushed).days

    participation = gh.get_stats(f"{base}/stats/participation")
    if participation and "all" in participation:
        weekly = participation["all"]
        activity.commits_last_year = sum(weekly)
        activity.active_weeks_last_year = sum(1 for week in weekly if week > 0)
    else:
        warnings.append("Commit participation stats not ready (GitHub still computing); rerun to fill in")

    releases = gh.get_optional(f"{base}/releases", {"per_page": 100}) or []
    activity.releases_count = len(releases)
    if releases:
        latest = releases[0]
        activity.latest_release_tag = latest.get("tag_name")
        dates = [
            datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
            for r in releases[:RELEASES_FOR_CADENCE]
            if r.get("published_at")
        ]
        if dates:
            activity.latest_release_at = dates[0]
        if len(dates) >= 2:
            gaps = [
                (dates[i] - dates[i + 1]).total_seconds() / 86400
                for i in range(len(dates) - 1)
            ]
            activity.mean_days_between_releases = round(sum(gaps) / len(gaps), 1)
    return activity


def _maintainership(
    gh: GitHubClient, base: str, owner: str, name: str, warnings: list[str]
) -> Maintainership:
    result = Maintainership()

    contributors = gh.get_optional(f"{base}/contributors", {"per_page": 100}) or []
    contributors = [c for c in contributors if c.get("type") != "Anonymous"]
    if contributors:
        result.contributors_sampled = len(contributors)
        counts = sorted((c.get("contributions", 0) for c in contributors), reverse=True)
        total = sum(counts)
        result.top_contributors = [
            Contributor(login=c.get("login", "?"), commits=c.get("contributions", 0))
            for c in contributors[:TOP_CONTRIBUTORS_SHOWN]
        ]
        if total > 0:
            result.top_contributor_share = round(counts[0] / total, 3)
            covered = 0
            for i, count in enumerate(counts, start=1):
                covered += count
                if covered >= total / 2:
                    result.bus_factor = i
                    break
    else:
        warnings.append("Contributor list unavailable")

    repo_query = f"repo:{owner}/{name}"
    issues = IssueMetrics(
        open_issues=gh.search_count(f"{repo_query} type:issue state:open"),
        closed_issues=gh.search_count(f"{repo_query} type:issue state:closed"),
        open_prs=gh.search_count(f"{repo_query} type:pr state:open"),
        merged_prs=gh.search_count(f"{repo_query} type:pr is:merged"),
    )
    if None in (issues.open_issues, issues.closed_issues, issues.open_prs, issues.merged_prs):
        warnings.append("Some issue/PR counts unavailable (search API limit); rerun later")
    if issues.open_issues is not None and issues.closed_issues is not None:
        total_issues = issues.open_issues + issues.closed_issues
        if total_issues > 0:
            issues.closed_ratio = round(issues.closed_issues / total_issues, 3)
    closed_prs = gh.search_count(f"{repo_query} type:pr state:closed")
    if closed_prs is not None and issues.merged_prs is not None:
        issues.closed_unmerged_prs = closed_prs - issues.merged_prs
    result.issues = issues
    return result


def _community(gh: GitHubClient, base: str, warnings: list[str]) -> CommunityHealth:
    profile = gh.get_optional(f"{base}/community/profile")
    if not profile:
        warnings.append("Community profile unavailable")
        return CommunityHealth()
    files = profile.get("files") or {}
    return CommunityHealth(
        health_percentage=profile.get("health_percentage"),
        has_readme=files.get("readme") is not None,
        has_license=files.get("license") is not None,
        has_contributing=files.get("contributing") is not None,
        has_code_of_conduct=files.get("code_of_conduct") is not None,
        has_issue_template=files.get("issue_template") is not None,
        has_pull_request_template=files.get("pull_request_template") is not None,
        has_description=bool(profile.get("description")),
    )


def _fetch_tree(
    gh: GitHubClient, base: str, default_branch: Optional[str], warnings: list[str]
) -> list[str]:
    """Fetch the full file tree of the default branch (list of paths)."""
    if not default_branch:
        return []
    tree = gh.get_optional(f"{base}/git/trees/{default_branch}", {"recursive": "1"})
    if not tree:
        warnings.append("File tree unavailable; file-based signals are incomplete")
        return []
    if tree.get("truncated"):
        warnings.append("File tree truncated by GitHub API; file-based signals may be incomplete")
    return [
        entry["path"] for entry in tree.get("tree", []) if entry.get("type") == "blob"
    ]


def _quality(paths: list[str]) -> QualitySignals:
    signals = QualitySignals()
    for path in paths:
        parts = path.split("/")
        filename = parts[-1]

        if path.startswith(".github/workflows/") and filename.endswith((".yml", ".yaml")):
            signals.has_ci = True
            signals.ci_workflows.append(filename)

        if not signals.has_tests:
            if any(part.lower() in TEST_DIR_NAMES for part in parts[:-1]):
                signals.has_tests = True
            elif any(fnmatch.fnmatch(filename, pattern) for pattern in TEST_FILE_PATTERNS):
                signals.has_tests = True

        if parts[0].lower() in ("docs", "doc") and len(parts) > 1:
            signals.has_docs_dir = True

        if filename in LINTER_CONFIG_NAMES and filename not in signals.linter_configs:
            signals.linter_configs.append(filename)

        if filename == ".editorconfig":
            signals.has_editorconfig = True
        if filename == ".pre-commit-config.yaml":
            signals.has_precommit_config = True

    signals.has_linter_config = bool(signals.linter_configs) or signals.has_precommit_config
    signals.ci_workflows.sort()
    signals.linter_configs.sort()
    return signals


def _security(paths: list[str], community: CommunityHealth) -> SecuritySignals:
    signals = SecuritySignals()
    for path in paths:
        filename = path.split("/")[-1]
        lower = path.lower()
        if lower in ("security.md", ".github/security.md", "docs/security.md"):
            signals.has_security_policy = True
        if lower in (".github/dependabot.yml", ".github/dependabot.yaml"):
            signals.has_dependabot_config = True
        if lower.startswith(".github/workflows/") and "codeql" in lower:
            signals.has_codeql_workflow = True
        if filename in LOCKFILE_NAMES and filename not in signals.lockfiles:
            signals.lockfiles.append(filename)
    signals.lockfiles.sort()
    return signals


# ---------------------------------------------------------------------------
# Organization scanning
# ---------------------------------------------------------------------------


def scan_organization(login: str, token: Optional[str] = None) -> OrgReport:
    """Scan a GitHub organization's public profile and repository portfolio."""
    warnings: list[str] = []
    with GitHubClient(token=token) as gh:
        try:
            org_raw = gh.get(f"/orgs/{login}")
        except RepoNotFoundError:
            user = gh.get_optional(f"/users/{login}")
            if user and user.get("type") == "User":
                raise GitHubError(
                    f"{login!r} is a user account, not an organization; "
                    "organization scanning covers organizations only"
                ) from None
            raise RepoNotFoundError(f"Organization {login!r} not found") from None

        data = OrgData(
            info=_org_info(org_raw),
            portfolio=_org_portfolio(gh, login, warnings),
        )
        return OrgReport(
            generated_at=datetime.now(timezone.utc),
            source=OrgRef(url=f"https://github.com/{login}", login=data.info.login),
            data=data,
            metrics=compute_org_metrics(data),
            warnings=warnings,
        )


def _org_info(raw: dict[str, Any]) -> OrgInfo:
    return OrgInfo(
        login=raw.get("login", "?"),
        name=raw.get("name"),
        description=raw.get("description"),
        blog=raw.get("blog") or None,
        location=raw.get("location"),
        email=raw.get("email"),
        twitter_username=raw.get("twitter_username"),
        is_verified=raw.get("is_verified", False),
        public_repos=raw.get("public_repos", 0),
        followers=raw.get("followers", 0),
        created_at=raw.get("created_at"),
        avatar_url=raw.get("avatar_url"),
    )


def _org_portfolio(gh: GitHubClient, login: str, warnings: list[str]) -> OrgPortfolio:
    portfolio = OrgPortfolio()

    repos = gh.get_optional(
        f"/orgs/{login}/repos", {"per_page": 100, "type": "public", "sort": "pushed"}
    )
    if repos is None:
        warnings.append("Organization repository list unavailable")
        repos = []

    now = datetime.now(timezone.utc)
    pushed_90 = pushed_365 = 0
    for repo in repos:
        pushed_at = repo.get("pushed_at")
        if pushed_at:
            age = (now - datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))).days
            if age <= 90:
                pushed_90 += 1
            if age <= 365:
                pushed_365 += 1
        if repo.get("fork"):
            portfolio.forks_sampled += 1
        else:
            portfolio.original_repos_sampled += 1
        portfolio.total_stars_sampled += repo.get("stargazers_count", 0)

    portfolio.repos_sampled = len(repos)
    if repos:
        portfolio.repos_pushed_90d = pushed_90
        portfolio.repos_pushed_365d = pushed_365
        portfolio.top_repos = [
            TopRepo(
                name=r.get("name", "?"),
                stars=r.get("stargazers_count", 0),
                pushed_at=r.get("pushed_at"),
                description=r.get("description"),
            )
            for r in sorted(repos, key=lambda r: r.get("stargazers_count", 0), reverse=True)[:5]
        ]

    members = gh.get_optional(f"/orgs/{login}/public_members", {"per_page": 100})
    if members is not None:
        portfolio.public_members = len(members)

    return portfolio


def _dependencies(paths: list[str]) -> DependencySignals:
    signals = DependencySignals()
    ecosystems: set[str] = set()
    manifests: set[str] = set()
    for path in paths:
        # Only look at the repo root and one level down to avoid picking up
        # vendored code and test fixtures.
        if path.count("/") > 1:
            continue
        filename = path.split("/")[-1]
        for pattern, ecosystem in MANIFEST_ECOSYSTEMS.items():
            if fnmatch.fnmatch(filename, pattern):
                manifests.add(path)
                ecosystems.add(ecosystem)
    signals.manifests = sorted(manifests)
    signals.ecosystems = sorted(ecosystems)
    return signals
