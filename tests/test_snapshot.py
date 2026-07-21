"""GraphQL snapshot: REST-shape mapping, fallback, and not-found parity."""

from __future__ import annotations

import pytest

from scanner.github import GitHubClient, GitHubError, RepoNotFoundError
from scanner.snapshot import RepoSnapshot, fetch_snapshot, _fetch_graphql


GRAPHQL_REPO = {
    "description": "The Python micro framework",
    "homepageUrl": "https://flask.palletsprojects.com",
    "hasWikiEnabled": False,
    "createdAt": "2010-04-06T11:11:59Z",
    "updatedAt": "2026-07-14T09:00:00Z",
    "pushedAt": "2026-07-13T20:15:00Z",
    "defaultBranchRef": {
        "name": "main",
        "target": {"history": {"nodes": [
            {
                "oid": "f" * 40,
                "messageHeadline": "Release 3.1.0",
                "messageBody": "",
                "committedDate": "2026-04-30T12:00:00Z",
                "author": {"name": "David Lord", "user": {"login": "davidism"}},
            },
        ]}},
    },
    "isFork": False,
    "isArchived": False,
    "isDisabled": False,
    "diskUsage": 10_240,
    "owner": {"login": "pallets", "__typename": "Organization"},
    "primaryLanguage": {"name": "Python"},
    "licenseInfo": {"spdxId": "BSD-3-Clause"},
    "stargazerCount": 68_000,
    "forkCount": 16_000,
    "watchers": {"totalCount": 2_100},
    "repositoryTopics": {"nodes": [{"topic": {"name": "flask"}}, {"topic": {"name": "wsgi"}}]},
    "languages": {"edges": [
        {"size": 900_000, "node": {"name": "Python"}},
        {"size": 12_000, "node": {"name": "HTML"}},
    ]},
    "releases": {"nodes": [
        {"tagName": "3.1.0", "publishedAt": "2026-05-01T00:00:00Z"},
        {"tagName": "3.0.0", "publishedAt": "2025-11-01T00:00:00Z"},
    ]},
    "tags": {"nodes": [
        {"name": "3.1.0", "target": {"committedDate": "2026-04-30T12:00:00Z"}},
        # Annotated tag: date nests one level deeper (Tag -> Commit).
        {"name": "3.0.0", "target": {"target": {"committedDate": "2025-10-31T12:00:00Z"}}},
    ]},
    "openIssues": {"totalCount": 5},
    "closedIssues": {"totalCount": 2_600},
    "openPRs": {"totalCount": 3},
    "mergedPRs": {"totalCount": 4_100},
    "closedPRs": {"totalCount": 4_500},
}


class FakeClient:
    """Just enough of GitHubClient for snapshot fetching."""

    def __init__(self, token="tok", graphql_result=None, graphql_error=None):
        self.token = token
        self._result = graphql_result
        self._error = graphql_error
        self.rest_calls: list[str] = []
        self.graphql_calls = 0

    def graphql(self, query, variables):
        self.graphql_calls += 1
        if self._error is not None:
            raise self._error
        return self._result

    def get(self, path, params=None, timeout=None):
        self.rest_calls.append(path)
        return {"full_name": "pallets/flask"}

    def get_optional(self, path, params=None, timeout=None):
        self.rest_calls.append(path)
        return {} if path.endswith("/languages") else []


def test_graphql_snapshot_maps_to_rest_shapes():
    gh = FakeClient(graphql_result={"repository": GRAPHQL_REPO})
    snapshot = fetch_snapshot(gh, "pallets", "flask", warnings := [])

    assert snapshot.via == "graphql"
    assert warnings == []
    repo = snapshot.repo
    assert repo["owner"] == {"login": "pallets", "type": "Organization"}
    assert repo["default_branch"] == "main"
    assert repo["homepage"] == "https://flask.palletsprojects.com"
    assert repo["has_wiki"] is False
    assert repo["fork"] is False and repo["archived"] is False and repo["disabled"] is False
    assert repo["size"] == 10_240
    assert repo["language"] == "Python"
    assert repo["topics"] == ["flask", "wsgi"]
    assert repo["license"]["spdx_id"] == "BSD-3-Clause"
    assert repo["stargazers_count"] == 68_000
    assert repo["forks_count"] == 16_000
    assert repo["subscribers_count"] == 2_100
    # REST open_issues_count spans issues AND pull requests.
    assert repo["open_issues_count"] == 5 + 3

    assert snapshot.languages == {"Python": 900_000, "HTML": 12_000}
    assert snapshot.releases == [
        {"tag_name": "3.1.0", "published_at": "2026-05-01T00:00:00Z"},
        {"tag_name": "3.0.0", "published_at": "2025-11-01T00:00:00Z"},
    ]
    counts = snapshot.issue_counts
    assert (counts.open_issues, counts.closed_issues) == (5, 2_600)
    # closed_prs uses REST search semantics (merged PRs count as closed), so
    # GraphQL's CLOSED (which excludes MERGED) is recombined with MERGED.
    assert (counts.open_prs, counts.merged_prs) == (3, 4_100)
    assert counts.closed_prs == 4_500 + 4_100

    assert snapshot.tags == [
        {"name": "3.1.0", "date": "2026-04-30T12:00:00Z"},
        {"name": "3.0.0", "date": "2025-10-31T12:00:00Z"},
    ]

    # Commit nodes are carried through verbatim; collect.py shapes them.
    assert [c["oid"] for c in snapshot.recent_commits] == ["f" * 40]


def test_graphql_nulls_map_like_rest_absences():
    raw = dict(GRAPHQL_REPO)
    raw.update(
        homepageUrl=None,
        defaultBranchRef=None,   # empty repository
        primaryLanguage=None,
        licenseInfo=None,
        repositoryTopics={"nodes": []},
        languages={"edges": []},
        releases={"nodes": []},
        tags={"nodes": []},
    )
    gh = FakeClient(graphql_result={"repository": raw})
    snapshot = fetch_snapshot(gh, "acme", "empty", [])
    assert snapshot.repo["default_branch"] is None
    assert snapshot.repo["language"] is None
    assert snapshot.repo["license"]["spdx_id"] is None
    assert snapshot.repo["topics"] == []
    assert snapshot.languages == {}
    assert snapshot.releases == []
    assert snapshot.tags == []
    # No default branch means no commits to read a history off.
    assert snapshot.recent_commits == []


def test_branch_ref_without_commit_target_yields_no_commits():
    # An empty repo whose ref target is not a Commit: the inline fragment
    # matches nothing and GraphQL returns a bare object.
    raw = dict(GRAPHQL_REPO, defaultBranchRef={"name": "main", "target": {}})
    snapshot = fetch_snapshot(FakeClient(graphql_result={"repository": raw}), "a", "b", [])
    assert snapshot.recent_commits == []


def test_rest_fallback_leaves_commits_unfetched():
    gh = FakeClient(graphql_error=GitHubError("HTTP 502"))
    snapshot = fetch_snapshot(gh, "pallets", "flask", [])
    # None, not []: the REST path never looked, rather than looking and finding none.
    assert snapshot.recent_commits is None


def test_no_token_uses_rest_without_warning():
    gh = FakeClient(token=None)
    snapshot = fetch_snapshot(gh, "pallets", "flask", warnings := [])
    assert snapshot.via == "rest"
    assert snapshot.issue_counts is None
    assert warnings == []
    assert gh.graphql_calls == 0
    assert gh.rest_calls == [
        "/repos/pallets/flask",
        "/repos/pallets/flask/languages",
        "/repos/pallets/flask/releases",
    ]


def test_graphql_failure_falls_back_to_rest_with_warning():
    gh = FakeClient(graphql_error=GitHubError("HTTP 502"))
    snapshot = fetch_snapshot(gh, "pallets", "flask", warnings := [])
    assert snapshot.via == "rest"
    assert gh.graphql_calls == 1
    assert len(warnings) == 1 and "fell back to REST" in warnings[0]


def test_graphql_not_found_propagates_without_rest_fallback():
    gh = FakeClient(graphql_error=RepoNotFoundError("Could not resolve"))
    with pytest.raises(RepoNotFoundError):
        fetch_snapshot(gh, "acme", "ghost", [])
    assert gh.rest_calls == []


def test_null_repository_without_error_is_not_found():
    gh = FakeClient(graphql_result={"repository": None})
    with pytest.raises(RepoNotFoundError):
        _fetch_graphql(gh, "acme", "ghost")
