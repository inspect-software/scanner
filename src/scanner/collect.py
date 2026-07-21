"""Collect public GitHub data for a repository and assemble a Report."""

from __future__ import annotations

import base64
import binascii
import fnmatch
import re
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple, Optional, Sequence

from .contacts import dedupe, from_owner_profile, from_security_policy
from .ecosystems import collect_ecosystem
from .github import GitHubClient, GitHubError, RepoNotFoundError, parse_repo_url
from .languages import significant_languages
from .license import license_for_report, normalize_spdx
from .metrics import compute_metrics, compute_org_metrics
from .sbom import collect_all_dependencies
from .runtime_deps import collect_runtime_closure, primary_package
from .vulns import collect_advisories
from .scorecard import run_scorecard as _run_scorecard
from .snapshot import IssueCounts, RepoSnapshot, fetch_snapshot
from .models import (
    Activity,
    AIReadinessSignals,
    CommunityHealth,
    ContactChannel,
    Contributor,
    ContributorOrganization,
    ContributorProfile,
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
    StarDay,
    StarHistory,
    TopRepo,
)


class TreeEntry(NamedTuple):
    """One blob in the repository tree: its path and size in bytes."""

    path: str
    size: int

TOP_CONTRIBUTORS_SHOWN = 10
RELEASES_FOR_CADENCE = 10
# Matches vX.Y.Z / X.Y.Z semver tags, optionally with a -prerelease/+build suffix.
SEMVER_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")

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
DOC_DIR_NAMES = {"doc", "docs", "documentation", "wiki"}
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
            snapshot = fetch_snapshot(gh, owner, name, warnings)
        except RepoNotFoundError:
            raise RepoNotFoundError(
                f"Repository {owner}/{name} not found or not publicly accessible"
            ) from None
        repo_data = snapshot.repo

        # The API's `full_name` is the repository's canonical identity: the
        # canonical casing (owner/repo names are case-insensitive), and after
        # a rename/transfer — which the API silently redirects — the name the
        # repository actually lives at now. Adopt it so the report describes
        # the repository GitHub served, not the spelling the URL used, and so
        # the remaining API calls skip the redirect hop.
        canonical = repo_data.get("full_name") or ""
        if "/" in canonical:
            owner, name = canonical.split("/", 1)
            source = RepoRef(url=repo_data.get("html_url") or url, owner=owner, name=name)
            base = f"/repos/{owner}/{name}"

        emit("Fetching owner profile…")
        contacts: list[ContactChannel] = []
        owner_profile = _owner_profile(gh, repo_data, warnings, contacts)
        repo_info = _repo_info(snapshot, warnings)
        popularity = _popularity(repo_data)

        emit("Collecting star history…")
        popularity.star_history = _star_history(gh, owner, name, popularity.stars, warnings)

        emit("Fetching activity and release history…")
        activity = _activity(gh, base, snapshot, warnings)

        emit("Fetching contributors and issue/PR counts…")
        maintainership = _maintainership(gh, base, owner, name, snapshot.issue_counts, warnings)

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

        if data.security_signals.has_security_policy:
            emit("Reading the security policy…")
            contacts += _security_contacts(gh, base, tree_paths, warnings)

        emit("Detecting ecosystem packages (PyPI, npm, etc.)…")
        packages, declared_dependencies, registry_contacts = collect_ecosystem(
            owner, name, repo_data.get("default_branch"), tree_paths, warnings
        )
        data.ecosystem = EcosystemData(packages=packages)
        data.dependencies.dependencies = declared_dependencies
        contacts += registry_contacts
        data.contacts = dedupe(contacts)

        emit("Collecting the full dependency graph (GitHub SBOM)…")
        data.dependencies.all_dependencies = collect_all_dependencies(
            gh, owner, name, declared_dependencies, warnings
        )

        # Advisory matching. Both sources are free, unauthenticated APIs and
        # cost no GitHub budget.
        #
        # Prefer the *published* package's runtime closure: it is what a
        # consumer installs. The repository graph is the fallback, and it
        # includes development and test pins that never ship — measuring
        # against it reports on a project's tooling as much as its software.
        if security_enabled:
            runtime = None
            published = primary_package(packages, name)
            if published is not None:
                emit(f"Resolving the runtime closure of {published.name}…")
                runtime = collect_runtime_closure(published, warnings)

            if runtime:
                emit("Matching runtime dependencies against known advisories (OSV)…")
                data.dependencies.advisories = collect_advisories(
                    runtime,
                    warnings,
                    scope="published_package",
                    assessed_package=(
                        f"{published.ecosystem}:{published.name}@{published.latest_version}"
                    ),
                )
            elif data.dependencies.all_dependencies.collected:
                emit("Matching dependencies against known advisories (OSV)…")
                data.dependencies.advisories = collect_advisories(
                    data.dependencies.all_dependencies.packages,
                    warnings,
                    total_packages=data.dependencies.all_dependencies.total_count,
                )

        emit("Analyzing AI-readiness signals…")
        data.ai_readiness = _ai_readiness(tree_entries, declared_dependencies)

        if run_scorecard and security_enabled:
            # Scorecard accepts one token. Use the client token that remained
            # active after any GitHub API rate-limit rotation.
            data.security_signals.scorecard = _run_scorecard(owner, name, gh.token, warnings, log=emit)

        # After Scorecard, so its License check can weigh in as a third source.
        data.license = license_for_report(data)

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
    gh: GitHubClient, repo_data: dict[str, Any], warnings: list[str],
    contacts: Optional[list[ContactChannel]] = None,
) -> Optional[OwnerProfile]:
    """Fetch the public profile of the account that owns the repository.

    Works for both organizations and personal (user) accounts — the /users/
    endpoint serves both and returns the owner's followers, public repo count
    and (for orgs) the verified-domain flag.

    The same payload carries the owner's published email and Twitter handle.
    Those are contact data, not profile facts: they are appended to
    ``contacts`` (kept out of the public report) rather than set on the
    returned ``OwnerProfile``, which is published.
    """
    owner = repo_data.get("owner") or {}
    login = owner.get("login")
    if not login:
        return None
    raw = gh.get_optional(f"/users/{login}")
    if not raw:
        warnings.append("Repository owner profile unavailable")
        return None

    if contacts is not None:
        contacts.extend(from_owner_profile(raw))

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
        location=raw.get("location"),
        followers=raw.get("followers", 0),
        public_repos=raw.get("public_repos", 0),
        created_at=created_at,
        account_age_days=age_days,
        # Only organizations expose is_verified; None for user accounts.
        is_verified=raw.get("is_verified") if owner_type == "Organization" else None,
        avatar_url=raw.get("avatar_url"),
    )


def _repo_info(snapshot: RepoSnapshot, warnings: list[str]) -> RepoInfo:
    data = snapshot.repo
    languages = snapshot.languages
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
        significant_languages=significant_languages(languages, data.get("language")),
        topics=data.get("topics") or [],
        # Recognized identifier only; `data.license` carries the full picture,
        # including the NOASSERTION case this deliberately drops.
        license_spdx=normalize_spdx(license_info.get("spdx_id")),
        license_spdx_raw=license_info.get("spdx_id"),
    )


def _popularity(data: dict[str, Any]) -> Popularity:
    return Popularity(
        stars=data.get("stargazers_count", 0),
        forks=data.get("forks_count", 0),
        watchers=data.get("subscribers_count", 0),
        open_issues_and_prs=data.get("open_issues_count", 0),
    )


# Star history is fetched newest-first, 100 stargazers per GraphQL page, and
# capped at this many pages so a repository with hundreds of thousands of stars
# cannot turn one scan into thousands of requests. 10 pages = up to 1000 star
# events, which captures the entire history of the vast majority of catalogue
# repositories (hundreds of stars) and, for a moderately popular one, a useful
# recent window (measured: ~7-11 months for 16k-53k-star repos). GraphQL draws
# from its own points/hour budget, separate from the REST limit the rest of the
# scan spends.
STAR_HISTORY_MAX_PAGES = 10

# Above this star count, skip star-history collection entirely. Two reasons,
# both measured: deep stargazer pagination gets slow on giant connections
# (~1.5s/page at 177k stars, ~15s for the 10-page cap), and the captured window
# shrinks to near-uselessness (yt-dlp's 1000 newest stars span 6 days). Popular
# package repositories sit well under this ceiling; the ones it excludes are the
# rare mega-repos where the fragment would neither be cheap nor chart well.
STAR_HISTORY_MAX_STARS = 100_000

STAR_HISTORY_QUERY = """
query StarHistory($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    stargazers(first: 100, after: $cursor, orderBy: {field: STARRED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      edges { starredAt }
    }
  }
}
"""


def _star_history(
    gh: GitHubClient, owner: str, name: str, total_stars: int, warnings: list[str]
) -> Optional[StarHistory]:
    """Collect per-day star additions for the stars-over-time chart.

    Best-effort and bounded (see STAR_HISTORY_MAX_PAGES): a missing token or a
    GraphQL failure returns None (with a warning) and never aborts a scan. Days
    are bucketed by UTC calendar day and returned ascending. ``complete`` is
    True when the whole history fit inside the page cap.
    """
    if not gh.token:
        return None  # the stargazers connection is only fetched via GraphQL
    if total_stars <= 0:
        return StarHistory(total_stars=0, collected=0, complete=True, days=[])
    if total_stars > STAR_HISTORY_MAX_STARS:
        # Too popular to page affordably, and the captured window would be too
        # short to be worth charting — skip rather than spend ~15s on a fragment.
        return None

    buckets: dict[str, int] = {}
    collected = 0
    cursor: Optional[str] = None
    try:
        for _page in range(STAR_HISTORY_MAX_PAGES):
            data = gh.graphql(
                STAR_HISTORY_QUERY, {"owner": owner, "name": name, "cursor": cursor}
            )
            connection = ((data.get("repository") or {}).get("stargazers")) or {}
            for edge in connection.get("edges") or []:
                starred_at = (edge or {}).get("starredAt")
                if starred_at:
                    day = starred_at[:10]
                    buckets[day] = buckets.get(day, 0) + 1
                    collected += 1
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    except GitHubError as exc:
        warnings.append(f"Star history unavailable: {exc}")
        if not buckets:
            return None

    days = [StarDay(date=day, count=count) for day, count in sorted(buckets.items())]
    return StarHistory(
        total_stars=total_stars,
        collected=collected,
        complete=collected >= total_stars,
        days=days,
    )


def _activity(
    gh: GitHubClient, base: str, snapshot: RepoSnapshot, warnings: list[str]
) -> Activity:
    activity = Activity()

    pushed_at = snapshot.repo.get("pushed_at")
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

    releases = snapshot.releases
    activity.releases_count = len(releases)
    if releases:
        latest = releases[0]
        activity.latest_release_tag = latest.get("tag_name")
        dates = [
            datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
            for r in releases[:RELEASES_FOR_CADENCE]
            if r.get("published_at")
        ]
        _apply_release_dates(activity, dates)
    else:
        # Many projects (e.g. illuminate/pipeline) tag versions but never cut
        # GitHub Releases. Fall back to semver tags so recency/cadence still
        # count; metric_release_discipline penalises the missing Releases.
        _activity_from_tags(gh, base, activity, snapshot)
    return activity


def _apply_release_dates(activity: Activity, dates: list[datetime]) -> None:
    """Fill recency/cadence from release/tag dates, newest first."""
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


def _semver_sort_key(name: str) -> tuple[int, int, int, int]:
    """Sort key for a semver tag; pre-release tags sort below their release."""
    core = name[1:] if name[:1] in ("v", "V") else name
    match = SEMVER_TAG_RE.match(name)
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    is_prerelease = "-" in core.split("+", 1)[0]
    return (major, minor, patch, 0 if is_prerelease else 1)


def _activity_from_tags(
    gh: GitHubClient, base: str, activity: Activity, snapshot: RepoSnapshot
) -> None:
    """Derive release facts from semver git tags when there are no Releases.

    The GraphQL snapshot already carries tags with resolved commit dates; the
    REST path fetches ``/tags`` and one commit per cadence tag on demand.
    """
    if snapshot.tags is not None:
        semver_tags = [t for t in snapshot.tags if SEMVER_TAG_RE.match(t["name"])]
    else:
        raw = gh.get_optional(f"{base}/tags", {"per_page": 100}) or []
        semver_tags = [
            {"name": t["name"], "commit_sha": (t.get("commit") or {}).get("sha")}
            for t in raw
            if t.get("name") and SEMVER_TAG_RE.match(t["name"])
        ]
    if not semver_tags:
        return

    semver_tags.sort(key=lambda t: _semver_sort_key(t["name"]), reverse=True)
    activity.releases_count = len(semver_tags)
    activity.releases_from_tags = True
    activity.latest_release_tag = semver_tags[0]["name"]

    dated: list[tuple[datetime, str]] = []
    for tag in semver_tags[:RELEASES_FOR_CADENCE]:
        iso = tag.get("date")
        if not iso and tag.get("commit_sha"):
            commit = gh.get_optional(f"{base}/commits/{tag['commit_sha']}")
            iso = (((commit or {}).get("commit") or {}).get("committer") or {}).get("date")
        if iso:
            dated.append(
                (datetime.fromisoformat(iso.replace("Z", "+00:00")), tag["name"])
            )
    if dated:
        dated.sort(key=lambda pair: pair[0], reverse=True)
        activity.latest_release_tag = dated[0][1]
        _apply_release_dates(activity, [pair[0] for pair in dated])


def _maintainership(
    gh: GitHubClient,
    base: str,
    owner: str,
    name: str,
    counts: Optional[IssueCounts],
    warnings: list[str],
) -> Maintainership:
    result = Maintainership()

    contributors = gh.get_optional(f"{base}/contributors", {"per_page": 100}) or []
    contributors = [c for c in contributors if c.get("type") != "Anonymous"]
    if contributors:
        result.contributors_sampled = len(contributors)
        commit_counts = sorted((c.get("contributions", 0) for c in contributors), reverse=True)
        total = sum(commit_counts)
        result.top_contributors = [
            Contributor(
                login=c.get("login", "?"),
                commits=c.get("contributions", 0),
                type=c.get("type"),
                avatar_url=c.get("avatar_url"),
            )
            for c in contributors[:TOP_CONTRIBUTORS_SHOWN]
        ]
        _enrich_top_contributors(gh, result.top_contributors, warnings)
        if total > 0:
            result.top_contributor_share = round(commit_counts[0] / total, 3)
            covered = 0
            for i, count in enumerate(commit_counts, start=1):
                covered += count
                if covered >= total / 2:
                    result.bus_factor = i
                    break
    else:
        warnings.append("Contributor list unavailable")

    if counts is None:
        counts = _search_issue_counts(gh, owner, name, warnings)
    issues = IssueMetrics(
        open_issues=counts.open_issues,
        closed_issues=counts.closed_issues,
        open_prs=counts.open_prs,
        merged_prs=counts.merged_prs,
    )
    if issues.open_issues is not None and issues.closed_issues is not None:
        total_issues = issues.open_issues + issues.closed_issues
        if total_issues > 0:
            issues.closed_ratio = round(issues.closed_issues / total_issues, 3)
    if counts.closed_prs is not None and issues.merged_prs is not None:
        issues.closed_unmerged_prs = counts.closed_prs - issues.merged_prs
    result.issues = issues
    return result


def _enrich_top_contributors(
    gh: GitHubClient,
    contributors: list[Contributor],
    warnings: list[str],
) -> None:
    """Add public person and organization facts in one GraphQL round trip.

    The REST contributors response already supplies login, account type and
    avatar. It does not include profile location/company or public organization
    memberships. Once the displayed top ten are known, aliases let one GraphQL
    request fetch all of those profiles. The enrichment is deliberately
    optional: unauthenticated scans and GraphQL failures keep the original
    contributor data and therefore the exact same bus-factor calculation.
    """
    if not getattr(gh, "token", None):
        return

    candidates = [
        contributor
        for contributor in contributors
        if contributor.login and contributor.login != "?" and contributor.type != "Bot"
    ]
    if not candidates:
        return

    declarations = ", ".join(f"$login{i}: String!" for i in range(len(candidates)))
    selections = "\n".join(
        f"""
        contributor{i}: user(login: $login{i}) {{
          name
          location
          company
          organizations(first: 20) {{ nodes {{ login name location }} }}
        }}
        """
        for i in range(len(candidates))
    )
    query = f"query ContributorProfiles({declarations}) {{\n{selections}\n}}"
    variables = {f"login{i}": contributor.login for i, contributor in enumerate(candidates)}

    try:
        data = gh.graphql(query, variables)
    except GitHubError as exc:
        warnings.append(f"Contributor profile enrichment unavailable: {exc}")
        return

    for i, contributor in enumerate(candidates):
        raw = data.get(f"contributor{i}")
        if not isinstance(raw, dict):
            continue
        organizations = [
            ContributorOrganization(
                login=node["login"], name=node.get("name"), location=node.get("location")
            )
            for node in (raw.get("organizations") or {}).get("nodes") or []
            if isinstance(node, dict) and node.get("login")
        ]
        contributor.profile = ContributorProfile(
            name=raw.get("name"),
            location=raw.get("location"),
            company=raw.get("company"),
            organizations=organizations,
        )


def _search_issue_counts(
    gh: GitHubClient, owner: str, name: str, warnings: list[str]
) -> IssueCounts:
    """Issue/PR totals via the search API — the REST fallback when the
    GraphQL snapshot (which carries exact totals) is unavailable."""
    repo_query = f"repo:{owner}/{name}"
    counts = IssueCounts(
        open_issues=gh.search_count(f"{repo_query} type:issue state:open"),
        closed_issues=gh.search_count(f"{repo_query} type:issue state:closed"),
        open_prs=gh.search_count(f"{repo_query} type:pr state:open"),
        merged_prs=gh.search_count(f"{repo_query} type:pr is:merged"),
        closed_prs=gh.search_count(f"{repo_query} type:pr state:closed"),
    )
    if None in (counts.open_issues, counts.closed_issues, counts.open_prs, counts.merged_prs):
        warnings.append("Some issue/PR counts unavailable (search API limit); rerun later")
    return counts


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

        if parts[0].lower() in DOC_DIR_NAMES and len(parts) > 1:
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


SECURITY_POLICY_PATHS = ("security.md", ".github/security.md", "docs/security.md")


def _security_policy_path(paths: list[str]) -> Optional[str]:
    """The repo's SECURITY.md as actually spelled in the tree, if present."""
    for path in paths:
        if path.lower() in SECURITY_POLICY_PATHS:
            return path
    return None


def _security_contacts(
    gh: GitHubClient, base: str, paths: list[str], warnings: list[str]
) -> list[ContactChannel]:
    """Read the disclosure route out of the repository's SECURITY.md.

    The file is fetched only when the tree says it exists, so this costs one
    request on repos that have a policy and none on those that do not. A policy
    that cannot be read is not a scan failure — the presence signal in
    ``_security`` stands on the tree alone and is unaffected.
    """
    path = _security_policy_path(paths)
    if not path:
        return []
    raw = gh.get_optional(f"{base}/contents/{path}")
    if not isinstance(raw, dict):
        warnings.append(f"Security policy '{path}' could not be read for contacts")
        return []
    if raw.get("encoding") != "base64" or not isinstance(raw.get("content"), str):
        return []
    try:
        text = base64.b64decode(raw["content"]).decode("utf-8", errors="replace")
    except (ValueError, binascii.Error):
        warnings.append(f"Security policy '{path}' could not be decoded")
        return []
    return from_security_policy(text, path)


def _security(paths: list[str], community: CommunityHealth) -> SecuritySignals:
    signals = SecuritySignals()
    for path in paths:
        filename = path.split("/")[-1]
        lower = path.lower()
        if lower in SECURITY_POLICY_PATHS:
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
