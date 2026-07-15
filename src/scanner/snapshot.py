"""Repository snapshot: the scan's bulk metadata in one GraphQL round trip.

The REST collection path spends eight requests per scan on data GraphQL can
return in a single query drawn from its own, separate rate-limit budget:

- ``/repos/{owner}/{name}``                      (core metadata)
- ``/repos/{owner}/{name}/languages``
- ``/repos/{owner}/{name}/releases?per_page=100``
- five ``/search/issues`` count queries          (worst offenders: the search
  API has its own ~30 requests/minute limit that back-to-back scans trip)

This module fetches those as one ``RepoSnapshot``. The GraphQL result is
mapped into the exact REST response shapes, so the collectors in
``collect.py`` consume one structure regardless of transport, and a REST
implementation of the same snapshot remains as the fallback — used for
unauthenticated scans (GraphQL requires a token) and whenever the GraphQL
request fails, so a scan never dies on the fast path.

Deliberately still REST (no GraphQL equivalent, or a known parity gap):
``/users/{login}`` (organization followers are absent from GraphQL),
``/stats/participation`` (weekly buckets), ``/contributors``,
``/community/profile`` (health_percentage), the recursive tree, and the SBOM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .github import GitHubClient, GitHubError, RepoNotFoundError

# Matches the REST path's ``per_page=100``: release history is sampled, and
# cadence uses the newest few (see collect.RELEASES_FOR_CADENCE).
MAX_RELEASES = 100
MAX_LANGUAGES = 100
MAX_TOPICS = 100

REPO_SNAPSHOT_QUERY = """
query RepoSnapshot($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    description
    homepageUrl
    hasWikiEnabled
    createdAt
    updatedAt
    pushedAt
    defaultBranchRef { name }
    isFork
    isArchived
    isDisabled
    diskUsage
    owner { login __typename }
    primaryLanguage { name }
    licenseInfo { spdxId }
    stargazerCount
    forkCount
    watchers { totalCount }
    repositoryTopics(first: %(topics)d) { nodes { topic { name } } }
    languages(first: %(languages)d, orderBy: {field: SIZE, direction: DESC}) {
      edges { size node { name } }
    }
    releases(first: %(releases)d, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { tagName publishedAt }
    }
    tags: refs(refPrefix: "refs/tags/", first: %(releases)d, orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
      nodes {
        name
        target {
          ... on Commit { committedDate }
          ... on Tag { target { ... on Commit { committedDate } } }
        }
      }
    }
    openIssues: issues(states: OPEN) { totalCount }
    closedIssues: issues(states: CLOSED) { totalCount }
    openPRs: pullRequests(states: OPEN) { totalCount }
    mergedPRs: pullRequests(states: MERGED) { totalCount }
    closedPRs: pullRequests(states: CLOSED) { totalCount }
  }
}
""" % {"topics": MAX_TOPICS, "languages": MAX_LANGUAGES, "releases": MAX_RELEASES}


@dataclass
class IssueCounts:
    """Issue/PR totals; None per field when a REST search query failed."""

    open_issues: Optional[int] = None
    closed_issues: Optional[int] = None
    open_prs: Optional[int] = None
    merged_prs: Optional[int] = None
    closed_prs: Optional[int] = None


@dataclass
class RepoSnapshot:
    """Bulk repository metadata in REST response shapes.

    ``repo`` mirrors ``GET /repos/{owner}/{name}``, ``languages`` mirrors
    ``/languages``, ``releases`` mirrors ``/releases`` (the consumed fields),
    so collectors are transport-agnostic. ``issue_counts`` is None when the
    snapshot came from REST — the caller then falls back to search queries.
    """

    repo: dict[str, Any]
    languages: dict[str, int] = field(default_factory=dict)
    releases: list[dict[str, Any]] = field(default_factory=list)
    issue_counts: Optional[IssueCounts] = None
    # Git tags as [{"name", "date"}] with commit dates already resolved —
    # GraphQL delivers them inside the same query, where the REST fallback
    # would need /tags plus one /commits/{sha} per tag. None on the REST
    # path: the tags-fallback collector then fetches on demand.
    tags: Optional[list[dict[str, Any]]] = None
    via: str = "rest"


def fetch_snapshot(
    gh: GitHubClient, owner: str, name: str, warnings: list[str]
) -> RepoSnapshot:
    """Fetch the repository snapshot, GraphQL first, REST as the safety net.

    Raises RepoNotFoundError (from either transport) for missing/private
    repositories; every other GraphQL failure downgrades to REST with a
    warning instead of failing the scan.
    """
    if gh.token:
        try:
            return _fetch_graphql(gh, owner, name)
        except RepoNotFoundError:
            raise
        except GitHubError as exc:
            warnings.append(f"GraphQL snapshot failed, fell back to REST: {exc}")
    return _fetch_rest(gh, owner, name)


def _fetch_graphql(gh: GitHubClient, owner: str, name: str) -> RepoSnapshot:
    data = gh.graphql(REPO_SNAPSHOT_QUERY, {"owner": owner, "name": name})
    raw = data.get("repository")
    if not raw:
        raise RepoNotFoundError(f"Repository {owner}/{name} not found")

    counts = IssueCounts(
        open_issues=raw["openIssues"]["totalCount"],
        closed_issues=raw["closedIssues"]["totalCount"],
        open_prs=raw["openPRs"]["totalCount"],
        merged_prs=raw["mergedPRs"]["totalCount"],
        # REST semantics (search state:closed) count merged PRs as closed;
        # GraphQL's CLOSED state excludes MERGED, so recombine to keep the
        # closed_unmerged_prs = closed - merged derivation working.
        closed_prs=raw["closedPRs"]["totalCount"] + raw["mergedPRs"]["totalCount"],
    )
    repo = {
        "owner": {
            "login": (raw.get("owner") or {}).get("login"),
            "type": (raw.get("owner") or {}).get("__typename"),
        },
        "description": raw.get("description"),
        "homepage": raw.get("homepageUrl"),
        "has_wiki": raw.get("hasWikiEnabled"),
        "created_at": raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
        "pushed_at": raw.get("pushedAt"),
        "default_branch": (raw.get("defaultBranchRef") or {}).get("name"),
        "fork": raw.get("isFork", False),
        "archived": raw.get("isArchived", False),
        "disabled": raw.get("isDisabled", False),
        "size": raw.get("diskUsage"),
        "language": (raw.get("primaryLanguage") or {}).get("name"),
        "topics": [
            node["topic"]["name"]
            for node in (raw.get("repositoryTopics") or {}).get("nodes") or []
            if node and node.get("topic")
        ],
        "license": {"spdx_id": (raw.get("licenseInfo") or {}).get("spdxId")},
        "stargazers_count": raw.get("stargazerCount", 0),
        "forks_count": raw.get("forkCount", 0),
        "subscribers_count": (raw.get("watchers") or {}).get("totalCount", 0),
        # REST's open_issues_count spans both issues and pull requests.
        "open_issues_count": counts.open_issues + counts.open_prs,
    }
    languages = {
        edge["node"]["name"]: edge["size"]
        for edge in (raw.get("languages") or {}).get("edges") or []
        if edge and edge.get("node")
    }
    releases = [
        {"tag_name": node.get("tagName"), "published_at": node.get("publishedAt")}
        for node in (raw.get("releases") or {}).get("nodes") or []
        if node
    ]
    tags = [
        {"name": node["name"], "date": _tag_commit_date(node.get("target"))}
        for node in (raw.get("tags") or {}).get("nodes") or []
        if node and node.get("name")
    ]
    return RepoSnapshot(
        repo=repo,
        languages=languages,
        releases=releases,
        issue_counts=counts,
        tags=tags,
        via="graphql",
    )


def _tag_commit_date(target: Optional[dict[str, Any]]) -> Optional[str]:
    """Committer date behind a tag ref — lightweight tags point straight at a
    commit, annotated tags nest one level deeper (Tag -> Commit)."""
    if not target:
        return None
    if "committedDate" in target:
        return target["committedDate"]
    nested = target.get("target") or {}
    return nested.get("committedDate")


def _fetch_rest(gh: GitHubClient, owner: str, name: str) -> RepoSnapshot:
    """The original per-endpoint collection, byte-compatible with pre-GraphQL scans."""
    base = f"/repos/{owner}/{name}"
    repo = gh.get(base)
    languages = gh.get_optional(f"{base}/languages") or {}
    releases = gh.get_optional(f"{base}/releases", {"per_page": MAX_RELEASES}) or []
    return RepoSnapshot(repo=repo, languages=languages, releases=releases, via="rest")
