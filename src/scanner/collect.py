"""Collect public GitHub data for a repository and assemble a Report."""

from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple, Optional, Sequence

from .ecosystems import collect_ecosystem
from .github import GitHubClient, GitHubError, RepoNotFoundError, parse_repo_url
from .metrics import compute_metrics, compute_org_metrics
from .scorecard import run_scorecard as _run_scorecard
from .models import (
    Activity,
    AIReadinessSignals,
    CommunityHealth,
    Contributor,
    Dependency,
    DependencySignals,
    EcosystemData,
    IssueMetrics,
    Maintainership,
    OrgData,
    OrgInfo,
    OrgPortfolio,
    OrgRef,
    OrgReport,
    OwnerProfile,
    Popularity,
    QualitySignals,
    RepoData,
    Report,
    RepoInfo,
    RepoRef,
    ScanConfig,
    SecuritySignals,
    TopRepo,
)


class TreeEntry(NamedTuple):
    """One blob in the repository tree: its path and size in bytes."""

    path: str
    size: int

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
    "gems.locked",
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

# --- AI readiness signal detection ----------------------------------------
# Agent-instruction files, matched by lowercased basename.
AGENT_INSTRUCTION_BASENAMES = {
    "claude.md",
    "agents.md",
    "agent.md",
    "gemini.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".goosehints",
    ".aider.conf.yml",
}
# Agent-instruction files matched by full (lowercased) path or path prefix.
AGENT_INSTRUCTION_PATHS = {
    ".github/copilot-instructions.md",
}
AGENT_INSTRUCTION_PREFIXES = (".cursor/rules/", ".github/instructions/")

LLMS_TXT_BASENAMES = {"llms.txt", "llms-full.txt"}

BOOTSTRAP_BASENAMES = {
    "makefile",
    "gnumakefile",
    "taskfile.yml",
    "taskfile.yaml",
    "justfile",
    ".justfile",
    "mise.toml",
    ".mise.toml",
    "noxfile.py",
}

TYPECHECK_BASENAMES = {
    "mypy.ini",
    ".mypy.ini",
    "pyrightconfig.json",
    "tsconfig.json",
    "jsconfig.json",
    "py.typed",
}

NIX_BASENAMES = {"flake.nix", "shell.nix", "default.nix"}
DOCKERFILE_BASENAMES = {"dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"}

API_SCHEMA_BASENAMES = {
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.yaml", "swagger.yml", "swagger.json",
    "asyncapi.yaml", "asyncapi.yml",
    "schema.graphql", "schema.graphqls",
}
API_SCHEMA_SUFFIXES = (".graphql", ".graphqls", ".proto", ".raml")

EXAMPLE_DIR_NAMES = {"examples", "example", "samples", "sample", "cookbook", "recipes", "demos"}

# Source-code extensions used for the file-size legibility signal.
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".scala",
    ".swift", ".m", ".mm", ".dart", ".ex", ".exs",
}
# Path segments that mark vendored / generated code, excluded from legibility.
VENDOR_SEGMENTS = {
    "node_modules", "vendor", ".venv", "venv", "dist", "build", "third_party",
    "site-packages", ".git", "generated", "__pycache__",
}
# A source file larger than this (~1,500 lines) strains an agent's context.
OVERSIZED_SOURCE_BYTES = 60_000


def scan_repository(
    url: str,
    token: str | Sequence[str] | None = None,
    config: Optional[ScanConfig] = None,
    run_scorecard: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Report:
    """Scan a public GitHub repository and return a populated Report.

    ``config`` selects which metrics/categories/components are scored; it
    defaults to the full methodology and is embedded in the returned Report.
    ``run_scorecard`` gates the (slow) OpenSSF Scorecard CLI; it is skipped
    automatically when the security metric is disabled or the binary is absent.
    ``log``, if given, receives a one-line progress message at each major
    phase — for callers that want to surface coarse scan progress (e.g. the
    website admin panel's job log) without instrumenting every API call.
    """
    emit = log or (lambda _msg: None)
    config = config or ScanConfig()
    owner, name = parse_repo_url(url)
    source = RepoRef(url=url, owner=owner, name=name)
    warnings: list[str] = []
    security_enabled = config.category_enabled("security") and config.metric_enabled(
        "security_posture"
    )

    emit(f"Fetching repository metadata for {owner}/{name}…")
    with GitHubClient(token=token) as gh:
        base = f"/repos/{owner}/{name}"
        try:
            repo_data = gh.get(base)
        except RepoNotFoundError:
            raise RepoNotFoundError(
                f"Repository {owner}/{name} not found or not publicly accessible"
            ) from None

        emit("Fetching owner profile…")
        owner_profile = _owner_profile(gh, repo_data, warnings)
        repo_info = _repo_info(gh, base, repo_data, warnings)
        popularity = _popularity(repo_data)

        emit("Fetching activity and release history…")
        activity = _activity(gh, base, repo_data, warnings)

        emit("Fetching contributors and issue/PR counts…")
        maintainership = _maintainership(gh, base, owner, name, warnings)

        emit("Fetching community health profile…")
        community = _community(gh, base, warnings)

        data = RepoData(
            owner=owner_profile,
            repo=repo_info,
            popularity=popularity,
            activity=activity,
            maintainership=maintainership,
            community=community,
        )

        emit("Fetching file tree…")
        tree_entries = _fetch_tree(gh, base, repo_data.get("default_branch"), warnings)
        tree_paths = [entry.path for entry in tree_entries]

        emit("Scanning quality and security signals in the file tree…")
        data.quality_signals = _quality(tree_paths)
        data.security_signals = _security(tree_paths, data.community)
        data.dependencies = _dependencies(tree_paths)

        emit("Detecting ecosystem packages (PyPI, npm, etc.)…")
        packages, declared_dependencies = collect_ecosystem(
            owner, name, repo_data.get("default_branch"), tree_paths, warnings
        )
        data.ecosystem = EcosystemData(packages=packages)
        data.dependencies.dependencies = declared_dependencies

        emit("Analyzing AI-readiness signals…")
        data.ai_readiness = _ai_readiness(tree_entries, declared_dependencies)

        if run_scorecard and security_enabled:
            # Scorecard accepts one token. Use the client token that remained
            # active after any GitHub API rate-limit rotation.
            data.security_signals.scorecard = _run_scorecard(owner, name, gh.token, warnings, log=emit)

        emit("Computing metrics…")
        report = Report(
            generated_at=datetime.now(timezone.utc),
            source=source,
            config=config,
            data=data,
            metrics=compute_metrics(data, config),
            warnings=warnings,
        )
        if warnings:
            emit(f"{len(warnings)} warning(s): " + "; ".join(warnings))
        emit("Scan complete.")
        return report


def _owner_profile(
    gh: GitHubClient, repo_data: dict[str, Any], warnings: list[str]
) -> Optional[OwnerProfile]:
    """Fetch the public profile of the account that owns the repository.

    Works for both organizations and personal (user) accounts — the /users/
    endpoint serves both and returns the owner's followers, public repo count
    and (for orgs) the verified-domain flag.
    """
    owner = repo_data.get("owner") or {}
    login = owner.get("login")
    if not login:
        return None
    raw = gh.get_optional(f"/users/{login}")
    if not raw:
        warnings.append("Repository owner profile unavailable")
        return None

    created_at = raw.get("created_at")
    age_days = None
    if created_at:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - created).days
    owner_type = raw.get("type", owner.get("type"))
    return OwnerProfile(
        login=login,
        type=owner_type,
        name=raw.get("name"),
        company=raw.get("company"),
        blog=raw.get("blog") or None,
        followers=raw.get("followers", 0),
        public_repos=raw.get("public_repos", 0),
        created_at=created_at,
        account_age_days=age_days,
        # Only organizations expose is_verified; None for user accounts.
        is_verified=raw.get("is_verified") if owner_type == "Organization" else None,
        avatar_url=raw.get("avatar_url"),
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
        has_wiki=data.get("has_wiki"),
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
            activity.days_since_latest_release = (
                datetime.now(timezone.utc) - dates[0]
            ).days
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
) -> list[TreeEntry]:
    """Fetch the full file tree of the default branch (blobs: path + size)."""
    if not default_branch:
        return []
    tree = gh.get_optional(f"{base}/git/trees/{default_branch}", {"recursive": "1"})
    if not tree:
        warnings.append("File tree unavailable; file-based signals are incomplete")
        return []
    if tree.get("truncated"):
        warnings.append("File tree truncated by GitHub API; file-based signals may be incomplete")
    return [
        TreeEntry(path=entry["path"], size=entry.get("size") or 0)
        for entry in tree.get("tree", [])
        if entry.get("type") == "blob"
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


def scan_organization(
    login: str, token: str | Sequence[str] | None = None, config: Optional[ScanConfig] = None
) -> OrgReport:
    """Scan a GitHub organization's public profile and repository portfolio.

    ``config`` selects which metrics/categories/components are scored; it
    defaults to the full methodology and is embedded in the returned report."""
    config = config or ScanConfig()
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
            config=config,
            data=data,
            metrics=compute_org_metrics(data, config),
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


def _is_vendored(path: str) -> bool:
    return any(part in VENDOR_SEGMENTS for part in path.split("/")[:-1])


def _ai_readiness(
    entries: list[TreeEntry], dependencies: list[Dependency]
) -> AIReadinessSignals:
    """Detect AI-development-readiness signals from the file tree.

    Path- and size-based only (no file contents): which agent-guidance,
    bootstrap, type-check, containerization and machine-interface files exist,
    and how large the source files are. See docs/metrics.md, "AI Readiness".
    """
    signals = AIReadinessSignals()
    agent_files: dict[str, int] = {}
    example_dirs: set[str] = set()
    has_notebook = False

    for path, size in entries:
        lower = path.lower()
        parts = lower.split("/")
        filename = parts[-1]

        # Agent guidance files (basename, exact path, or known prefix).
        is_agent = (
            filename in AGENT_INSTRUCTION_BASENAMES
            or lower in AGENT_INSTRUCTION_PATHS
            or any(lower.startswith(p) for p in AGENT_INSTRUCTION_PREFIXES)
        )
        if is_agent:
            agent_files[path] = size

        if filename in LLMS_TXT_BASENAMES:
            signals.has_llms_txt = True
        if filename in BOOTSTRAP_BASENAMES and path not in signals.bootstrap_files:
            signals.bootstrap_files.append(path)
        if filename in TYPECHECK_BASENAMES and path not in signals.typecheck_configs:
            signals.typecheck_configs.append(path)
        if filename in NIX_BASENAMES:
            signals.has_nix = True
        if filename in DOCKERFILE_BASENAMES or filename.startswith("dockerfile."):
            signals.has_dockerfile = True
        if parts[0] == ".devcontainer" or filename == "devcontainer.json":
            signals.has_devcontainer = True

        if (
            filename in API_SCHEMA_BASENAMES
            or filename.endswith(API_SCHEMA_SUFFIXES)
        ) and path not in signals.api_schema_files:
            signals.api_schema_files.append(path)

        if filename in ("mcp.json", ".mcp.json") or "mcp_server" in filename or "mcp-server" in filename:
            signals.has_mcp_signal = True

        if filename.endswith(".ipynb") and not _is_vendored(path):
            has_notebook = True
        for part in parts[:-1]:
            if part in EXAMPLE_DIR_NAMES:
                example_dirs.add(part)

        # Source-file size distribution (agent context legibility).
        ext = filename[filename.rfind("."):] if "." in filename else ""
        if ext in CODE_EXTENSIONS and not _is_vendored(path):
            signals.source_files_sampled += 1
            if signals.largest_source_bytes is None or size > signals.largest_source_bytes:
                signals.largest_source_bytes = size
            if size > OVERSIZED_SOURCE_BYTES:
                signals.oversized_source_files += 1

    # MCP is often declared as a dependency rather than a config file.
    if not signals.has_mcp_signal:
        for dep in dependencies:
            name = dep.name.lower()
            if name == "mcp" or "modelcontextprotocol" in name or name.endswith("mcp"):
                signals.has_mcp_signal = True
                break

    if has_notebook:
        example_dirs.add("notebooks")

    signals.agent_instruction_files = sorted(agent_files)
    signals.agent_instruction_max_bytes = max(agent_files.values()) if agent_files else None
    signals.bootstrap_files.sort()
    signals.typecheck_configs.sort()
    signals.api_schema_files.sort()
    signals.example_dirs = sorted(example_dirs)
    return signals
