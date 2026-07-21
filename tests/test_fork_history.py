"""Fork-history collection: bucketing, pagination cap, ceiling, and fallbacks."""

from __future__ import annotations

from scanner.collect import FORK_HISTORY_MAX_FORKS, FORK_HISTORY_MAX_PAGES, _fork_history
from scanner.github import GitHubError


def _page(created_at, has_next, cursor="c"):
    """Build one GraphQL forks page from a list of ISO timestamps."""
    return {
        "repository": {
            "forks": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor if has_next else None},
                "nodes": [{"createdAt": ts} for ts in created_at],
            }
        }
    }


class FakeClient:
    """Serves a queued sequence of GraphQL pages and counts calls."""

    def __init__(self, pages=None, token="tok", error=None):
        self.token = token
        self._pages = list(pages or [])
        self._error = error
        self.graphql_calls = 0

    def graphql(self, query, variables):
        self.graphql_calls += 1
        if self._error is not None:
            raise self._error
        if not self._pages:
            return _page([], has_next=False)
        return self._pages.pop(0)


def test_no_token_returns_none():
    gh = FakeClient(token=None)
    assert _fork_history(gh, "o", "n", 10, []) is None
    assert gh.graphql_calls == 0


def test_zero_forks_is_complete_and_empty():
    gh = FakeClient()
    fh = _fork_history(gh, "o", "n", 0, [])
    assert fh is not None and fh.complete is True and fh.days == [] and fh.collected == 0
    assert gh.graphql_calls == 0


def test_ceiling_skips_giant_repos_without_calling():
    gh = FakeClient()
    assert _fork_history(gh, "o", "n", FORK_HISTORY_MAX_FORKS + 1, []) is None
    assert gh.graphql_calls == 0


def test_single_page_complete_history_buckets_by_day():
    gh = FakeClient(pages=[
        _page(
            [
                "2025-01-01T09:00:00Z",
                "2025-01-01T18:30:00Z",  # same UTC day -> counts together
                "2025-01-03T12:00:00Z",
            ],
            has_next=False,
        )
    ])
    fh = _fork_history(gh, "o", "n", 3, warnings := [])
    assert gh.graphql_calls == 1
    assert fh.complete is True
    assert fh.collected == 3
    assert [(d.date, d.count) for d in fh.days] == [("2025-01-01", 2), ("2025-01-03", 1)]
    assert warnings == []


def test_pagination_follows_cursor_until_exhausted():
    gh = FakeClient(pages=[
        _page(["2025-02-01T00:00:00Z"], has_next=True, cursor="c1"),
        _page(["2025-01-01T00:00:00Z"], has_next=False),
    ])
    fh = _fork_history(gh, "o", "n", 2, [])
    assert gh.graphql_calls == 2
    assert fh.complete is True
    assert [(d.date, d.count) for d in fh.days] == [("2025-01-01", 1), ("2025-02-01", 1)]


def test_page_cap_truncates_and_marks_incomplete():
    always_more = [_page([f"2025-03-{i+1:02d}T00:00:00Z"], has_next=True, cursor=f"c{i}")
                   for i in range(FORK_HISTORY_MAX_PAGES + 5)]
    gh = FakeClient(pages=always_more)
    fh = _fork_history(gh, "o", "n", 10_000, [])
    assert gh.graphql_calls == FORK_HISTORY_MAX_PAGES
    assert fh.collected == FORK_HISTORY_MAX_PAGES  # one fork per page here
    assert fh.complete is False  # collected << total_forks


def test_counter_above_listable_entries_still_completes():
    # Observed in production: Nayjest/Gito reports forkCount 34 while the forks
    # connection lists 33 (a fork of a deleted account). Exhausting the
    # connection means the history is complete.
    gh = FakeClient(pages=[_page(["2025-05-01T00:00:00Z"], has_next=False)])
    fh = _fork_history(gh, "o", "n", 5, [])
    assert fh.collected == 1
    assert fh.complete is True


def test_graphql_error_with_partial_data_returns_what_it_has():
    class FailingSecondCall(FakeClient):
        def graphql(self, query, variables):
            self.graphql_calls += 1
            if self.graphql_calls == 1:
                return _page(["2025-04-01T00:00:00Z"], has_next=True, cursor="c1")
            raise GitHubError("boom")

    gh = FailingSecondCall()
    fh = _fork_history(gh, "o", "n", 50, warnings := [])
    assert fh is not None
    assert fh.collected == 1
    assert fh.complete is False
    assert any("Fork history unavailable" in w for w in warnings)


def test_graphql_error_before_any_data_returns_none():
    gh = FakeClient(error=GitHubError("boom"))
    fh = _fork_history(gh, "o", "n", 50, warnings := [])
    assert fh is None
    assert any("Fork history unavailable" in w for w in warnings)
