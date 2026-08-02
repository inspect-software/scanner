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

GraphQL-only (no REST equivalent is fetched, so the fallback path simply goes
without it): ``recent_commits``, the newest commits of the default branch; the
decided-pull-request sample behind the windowed contribution rates; and the
README markup, which costs nothing here because the blob rides along in the
same query that a REST path would need a separate ``/readme`` request for.

One further GraphQL request is made per scan, separately:
``probe_author_merge_counts`` batches every author lookup the newcomer figures
need into a single aliased ``search`` query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .github import GitHubClient, GitHubError, RepoNotFoundError

# GitHub logins are ASCII alphanumeric with interior hyphens. Anything else did
# not come from GitHub and must not be interpolated into a search query.
_SAFE_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

# Matches the REST path's ``per_page=100``: release history is sampled, and
# cadence uses the newest few (see collect.RELEASES_FOR_CADENCE).
MAX_RELEASES = 100
MAX_LANGUAGES = 100
MAX_TOPICS = 100

# Newest commits on the default branch, carried by the snapshot query rather
# than a request of their own: measured, the extra connection costs 0 rate-limit
# points (cost is per connection-request, and 500 nodes still round to 1 point)
# and 0.5-2.5s of latency. There is no REST counterpart here — the fallback path
# leaves recent_commits None rather than spending a request on it.
MAX_RECENT_COMMITS = 100

# Longest-open issues and pull requests carried for the contribution-flow
# facts. Twenty is enough to establish that a queue is unattended without
# turning the snapshot into a tracker export: the signals derived from them
# are counts over a threshold, and a repository with more than twenty items
# older than the threshold is already past every bar the assessment sets.
MAX_TRACKED_ITEMS = 20

# Merged pull requests sampled to find the most recent merge. GraphQL cannot
# order pull requests by merge date, so the newest-updated ones are read and
# the latest mergedAt among them taken: merging updates a pull request, so a
# recent merge sorts near the top, and a handful of stale-but-commented ones
# displacing it cannot hide a merge that actually happened.
MAX_MERGED_PR_SAMPLE = 10

# Decided (merged or closed-unmerged) pull requests read for the windowed
# outcome rates. GraphQL cannot filter a connection by date, so a fixed page of
# the most recently updated ones is taken and the windows are cut from it
# locally. Sixty covers a full month for all but the busiest few percent of
# repositories; past that the sample runs out inside the window, which
# ``sample_exhausted`` records so the counts are read as lower bounds. Raising
# it costs nothing in rate-limit points but does cost latency, and the ratios —
# the part that is scored — are already stable at this size.
MAX_DECIDED_PR_SAMPLE = 60

# Distinct authors whose merge history is checked per scan. Each is one aliased
# search node inside a single request, so the cap is about that request's cost,
# not about round trips. A repository with more than a dozen distinct people
# landing pull requests in a month has demonstrated an open door well past any
# threshold here, so the precision lost at the cap does not change a score.
MAX_AUTHOR_PROBES = 12

REPO_SNAPSHOT_QUERY = """
query RepoSnapshot($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    url
    description
    homepageUrl
    hasWikiEnabled
    createdAt
    updatedAt
    pushedAt
    defaultBranchRef {
      name
      target {
        ... on Commit {
          history(first: %(commits)d) {
            nodes {
              oid
              messageHeadline
              messageBody
              committedDate
              author { name email user { login } }
            }
          }
          checkSuites(last: 1) { nodes { conclusion updatedAt } }
        }
      }
    }
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
    recentlyMergedPRs: pullRequests(
      states: MERGED, first: %(merged)d, orderBy: {field: UPDATED_AT, direction: DESC}
    ) { nodes { mergedAt } }
    decidedPRs: pullRequests(
      states: [MERGED, CLOSED], first: %(decided)d, orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      nodes {
        number
        mergedAt
        closedAt
        author { login __typename }
      }
    }
    readmeMd: object(expression: "HEAD:README.md") { ...readmeBlob }
    readmeLower: object(expression: "HEAD:readme.md") { ...readmeBlob }
    readmeMdx: object(expression: "HEAD:README.mdx") { ...readmeBlob }
    readmeRst: object(expression: "HEAD:README.rst") { ...readmeBlob }
    readmeTxt: object(expression: "HEAD:README.txt") { ...readmeBlob }
    readmeBare: object(expression: "HEAD:README") { ...readmeBlob }
    oldestOpenPRs: pullRequests(
      states: OPEN, first: %(tracked)d, orderBy: {field: CREATED_AT, direction: ASC}
    ) {
      nodes {
        number
        createdAt
        comments(last: 1) { nodes { createdAt author { login } } }
      }
    }
    oldestOpenIssues: issues(
      states: OPEN, first: %(tracked)d, orderBy: {field: CREATED_AT, direction: ASC}
    ) {
      nodes {
        number
        createdAt
        comments(last: 1) { nodes { createdAt author { login } } }
      }
    }
    openIssues: issues(states: OPEN) { totalCount }
    closedIssues: issues(states: CLOSED) { totalCount }
    openPRs: pullRequests(states: OPEN) { totalCount }
    mergedPRs: pullRequests(states: MERGED) { totalCount }
    closedPRs: pullRequests(states: CLOSED) { totalCount }
  }
}

fragment readmeBlob on Blob { text }
""" % {
    "topics": MAX_TOPICS,
    "languages": MAX_LANGUAGES,
    "releases": MAX_RELEASES,
    "commits": MAX_RECENT_COMMITS,
    "tracked": MAX_TRACKED_ITEMS,
    "merged": MAX_MERGED_PR_SAMPLE,
    "decided": MAX_DECIDED_PR_SAMPLE,
}

# GitHub does not expose "the README" as a field; it resolves the name by
# convention at render time. The candidates below are read in order and the
# first one that exists wins, which reproduces that convention closely enough —
# a project whose README is named something else entirely is rare, and the
# report then simply records no badges rather than the wrong ones.
README_ALIASES = (
    "readmeMd",
    "readmeLower",
    "readmeMdx",
    "readmeRst",
    "readmeTxt",
    "readmeBare",
)

# One aliased search node per author, all in a single request. `search` with
# `type: ISSUE` is the only place GraphQL will filter pull requests by author,
# and issueCount is a total rather than a page, so no pagination is involved.
AUTHOR_MERGE_COUNT_QUERY = """
query AuthorMergeCounts(%(params)s) {
%(fields)s
}
"""


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
    # Newest-first commits from the default branch, GraphQL field names kept
    # verbatim. None on the REST path (no equivalent is fetched) and [] for an
    # empty repository — collect.py distinguishes neither, both yield no
    # recent_commits in the report.
    recent_commits: Optional[list[dict[str, Any]]] = None
    # Contribution-flow facts (merge recency, the longest-open issues and pull
    # requests, last CI run). None on the REST path, which fetches no
    # equivalent — the report then records the block as not collected rather
    # than as an empty tracker, and nothing derived from it is scored.
    contribution: Optional[dict[str, Any]] = None
    # README markup of the default branch, or None when the snapshot came from
    # REST (which fetches no equivalent) or the repository has no README under
    # any conventional name. The text is analysed during collection and thrown
    # away: a report carrying 31k READMEs verbatim would be mostly README.
    readme: Optional[str] = None
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
        # GitHub resolves renames and casing, so nameWithOwner is the spelling
        # the repository actually lives at now — not the one the submitted URL
        # used. collect.adopt_canonical_identity reads these two keys; without
        # them it never fired on this path, which is every scan with a token.
        "full_name": raw.get("nameWithOwner"),
        "html_url": raw.get("url"),
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
        recent_commits=_commit_history(raw.get("defaultBranchRef")),
        contribution=_contribution_flow(raw),
        readme=_readme_text(raw),
        via="graphql",
    )


def _readme_text(raw: dict[str, Any]) -> Optional[str]:
    """The first README the repository actually has, by conventional name."""
    for alias in README_ALIASES:
        blob = raw.get(alias)
        # A non-Blob object (a directory named README) matches the query but
        # the inline fragment leaves it empty; a binary blob has a null text.
        if isinstance(blob, dict) and blob.get("text"):
            return blob["text"]
    return None


def _tracked_items(connection: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open issues/pull requests as {number, created_at, last comment}."""
    items = []
    for node in (connection or {}).get("nodes") or []:
        if not node or node.get("number") is None:
            continue
        comments = (node.get("comments") or {}).get("nodes") or []
        last = comments[-1] if comments else None
        items.append(
            {
                "number": node["number"],
                "created_at": node.get("createdAt"),
                "last_comment_at": (last or {}).get("createdAt"),
                # A deleted account leaves the comment but drops the author.
                "last_comment_author": ((last or {}).get("author") or {}).get("login"),
            }
        )
    return items


def _contribution_flow(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge recency, the unattended ends of both queues, and the last CI run."""
    merged = [
        node["mergedAt"]
        for node in (raw.get("recentlyMergedPRs") or {}).get("nodes") or []
        if node and node.get("mergedAt")
    ]
    target = ((raw.get("defaultBranchRef") or {}).get("target")) or {}
    suites = [n for n in (target.get("checkSuites") or {}).get("nodes") or [] if n]
    suite = suites[-1] if suites else {}
    return {
        "last_merged_pr_at": max(merged) if merged else None,
        "open_prs": _tracked_items(raw.get("oldestOpenPRs")),
        "open_issues": _tracked_items(raw.get("oldestOpenIssues")),
        "ci_last_run_at": suite.get("updatedAt"),
        "ci_last_conclusion": suite.get("conclusion"),
        "decided_prs": _decided_prs(raw.get("decidedPRs")),
    }


def _decided_prs(connection: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decided pull requests as {number, decided_at, merged, author}.

    ``decided_at`` collapses mergedAt and closedAt into the one date the
    windowing cares about. GitHub sets closedAt on merged pull requests too, so
    merge status is read from mergedAt alone rather than inferred from which
    date is present.
    """
    items = []
    for node in (connection or {}).get("nodes") or []:
        if not node:
            continue
        merged_at = node.get("mergedAt")
        decided_at = merged_at or node.get("closedAt")
        if not decided_at:
            continue
        author = node.get("author") or {}
        items.append(
            {
                "number": node.get("number"),
                "decided_at": decided_at,
                "merged": merged_at is not None,
                # A deleted account leaves the pull request but drops the author.
                "author": author.get("login"),
                "author_type": author.get("__typename"),
            }
        )
    return items


def probe_author_merge_counts(
    gh: GitHubClient, owner: str, name: str, logins: list[str]
) -> dict[str, int]:
    """All-time merged pull requests in this repository, per author login.

    One request regardless of how many logins are asked about. Returns only the
    logins that resolved: a caller must treat a missing key as unknown, not as
    zero, or a failed probe would promote every established contributor to a
    first-timer.
    """
    safe = [login for login in logins if _SAFE_LOGIN.match(login or "")][:MAX_AUTHOR_PROBES]
    if not safe:
        return {}
    params = ", ".join(f"$q{i}: String!" for i in range(len(safe)))
    fields = "\n".join(
        f'  a{i}: search(query: $q{i}, type: ISSUE) {{ issueCount }}' for i in range(len(safe))
    )
    variables = {
        f"q{i}": f"repo:{owner}/{name} type:pr is:merged author:{login}"
        for i, login in enumerate(safe)
    }
    query = AUTHOR_MERGE_COUNT_QUERY % {"params": params, "fields": fields}
    data = gh.graphql(query, variables)
    counts = {}
    for i, login in enumerate(safe):
        node = data.get(f"a{i}")
        if isinstance(node, dict) and isinstance(node.get("issueCount"), int):
            counts[login] = node["issueCount"]
    return counts


def _commit_history(branch_ref: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Commit nodes behind the default branch ref.

    Returns [] for an empty repository (no ref, or a ref whose target is not a
    Commit — the inline fragment then yields an empty object).
    """
    target = (branch_ref or {}).get("target") or {}
    return [node for node in (target.get("history") or {}).get("nodes") or [] if node]


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
