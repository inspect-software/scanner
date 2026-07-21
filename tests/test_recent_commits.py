"""Recent-commit collection: mapping, body cap, and the no-history fallbacks."""

from __future__ import annotations

from scanner.collect import COMMIT_BODY_MAX_CHARS, _recent_commits
from scanner.snapshot import RepoSnapshot


def _node(oid="a" * 40, headline="Fix the thing", body=None, login="octocat", name="Octo Cat"):
    return {
        "oid": oid,
        "messageHeadline": headline,
        "messageBody": body,
        "committedDate": "2026-07-20T10:00:00Z",
        "author": {"name": name, "user": {"login": login} if login else None},
    }


def _snapshot(nodes):
    return RepoSnapshot(repo={}, recent_commits=nodes)


def test_maps_graphql_nodes_to_records():
    commit = _recent_commits(_snapshot([_node(body="Refs #12345.")]))[0]
    assert commit.oid == "a" * 40
    assert commit.headline == "Fix the thing"
    assert commit.body == "Refs #12345."
    assert commit.body_truncated is False
    assert commit.author_login == "octocat"
    assert commit.author_name == "Octo Cat"
    assert commit.committed_at.year == 2026


def test_order_is_preserved_newest_first():
    nodes = [_node(oid="1" * 40), _node(oid="2" * 40), _node(oid="3" * 40)]
    assert [c.oid[0] for c in _recent_commits(_snapshot(nodes))] == ["1", "2", "3"]


def test_empty_body_becomes_none_not_empty_string():
    # GraphQL returns "" for a single-line commit message; the report says None.
    commit = _recent_commits(_snapshot([_node(body="")]))[0]
    assert commit.body is None
    assert commit.body_truncated is False


def test_long_body_is_capped_and_flagged():
    commit = _recent_commits(_snapshot([_node(body="x" * (COMMIT_BODY_MAX_CHARS + 1))]))[0]
    assert len(commit.body) == COMMIT_BODY_MAX_CHARS
    assert commit.body_truncated is True


def test_body_exactly_at_the_cap_is_not_flagged():
    commit = _recent_commits(_snapshot([_node(body="x" * COMMIT_BODY_MAX_CHARS)]))[0]
    assert commit.body_truncated is False


def test_unlinked_author_keeps_the_git_name():
    # Commits by an address with no GitHub account have author.user == null.
    commit = _recent_commits(_snapshot([_node(login=None)]))[0]
    assert commit.author_login is None
    assert commit.author_name == "Octo Cat"


def test_rest_snapshot_yields_no_commits():
    # recent_commits is None on the REST path — no request is spent on it.
    assert _recent_commits(RepoSnapshot(repo={})) == []


def test_empty_repository_yields_no_commits():
    assert _recent_commits(_snapshot([])) == []
