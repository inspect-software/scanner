"""Star-history collection: bucketing, pagination cap, ceiling, and fallbacks."""

from __future__ import annotations

import pytest

from datetime import datetime, timezone

from scanner.collect import (
    STAR_HISTORY_MAX_PAGES,
    STAR_HISTORY_MAX_STARS,
    _carry_forward_star_history,
    _star_history,
)
from scanner.models import StarDay, StarHistory
from scanner.github import GitHubError


def _page(starred_at, has_next, cursor="c"):
    """Build one GraphQL stargazers page from a list of ISO timestamps."""
    return {
        "repository": {
            "stargazers": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor if has_next else None},
                "edges": [{"starredAt": ts} for ts in starred_at],
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
    assert _star_history(gh, "o", "n", 10, []) is None
    assert gh.graphql_calls == 0


def test_zero_stars_is_complete_and_empty():
    gh = FakeClient()
    sh = _star_history(gh, "o", "n", 0, [])
    assert sh is not None and sh.complete is True and sh.days == [] and sh.collected == 0
    assert gh.graphql_calls == 0


def test_ceiling_skips_giant_repos_without_calling():
    gh = FakeClient()
    assert _star_history(gh, "o", "n", STAR_HISTORY_MAX_STARS + 1, []) is None
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
    sh = _star_history(gh, "o", "n", 3, warnings := [])
    assert gh.graphql_calls == 1
    assert sh.complete is True
    assert sh.collected == 3
    # Ascending by date, same-day stars summed.
    assert [(d.date, d.count) for d in sh.days] == [("2025-01-01", 2), ("2025-01-03", 1)]
    assert warnings == []


def test_pagination_follows_cursor_until_exhausted():
    gh = FakeClient(pages=[
        _page(["2025-02-01T00:00:00Z"], has_next=True, cursor="c1"),
        _page(["2025-01-01T00:00:00Z"], has_next=False),
    ])
    sh = _star_history(gh, "o", "n", 2, [])
    assert gh.graphql_calls == 2
    assert sh.complete is True
    assert [(d.date, d.count) for d in sh.days] == [("2025-01-01", 1), ("2025-02-01", 1)]


def test_page_cap_truncates_and_marks_incomplete():
    # Every page reports another page available: the cap must stop it.
    always_more = [_page([f"2025-03-{i+1:02d}T00:00:00Z"], has_next=True, cursor=f"c{i}")
                   for i in range(STAR_HISTORY_MAX_PAGES + 5)]
    gh = FakeClient(pages=always_more)
    sh = _star_history(gh, "o", "n", 10_000, [])
    assert gh.graphql_calls == STAR_HISTORY_MAX_PAGES
    assert sh.collected == STAR_HISTORY_MAX_PAGES  # one star per page here
    assert sh.complete is False  # collected << total_stars


def test_counter_above_listable_entries_still_completes():
    # stargazerCount can exceed what the connection will list (deleted or
    # suspended accounts). Reaching the end of the connection is what makes the
    # history complete — comparing against the counter reports a full history
    # as truncated, which wrongly shows the "recent window only" caveat.
    gh = FakeClient(pages=[_page(["2025-05-01T00:00:00Z"], has_next=False)])
    sh = _star_history(gh, "o", "n", 5, [])
    assert sh.collected == 1
    assert sh.complete is True


def test_graphql_error_with_partial_data_returns_what_it_has():
    class FailingSecondCall(FakeClient):
        def graphql(self, query, variables):
            self.graphql_calls += 1
            if self.graphql_calls == 1:
                return _page(["2025-04-01T00:00:00Z"], has_next=True, cursor="c1")
            raise GitHubError("boom")

    gh = FailingSecondCall()
    sh = _star_history(gh, "o", "n", 50, warnings := [])
    assert sh is not None
    assert sh.collected == 1
    assert sh.complete is False
    assert any("Star history unavailable" in w for w in warnings)


def test_graphql_error_before_any_data_returns_none():
    gh = FakeClient(error=GitHubError("boom"))
    sh = _star_history(gh, "o", "n", 50, warnings := [])
    assert sh is None
    assert any("Star history unavailable" in w for w in warnings)


# ---------------------------------------------------------------------------
# Carry-forward: GitHub closed the stargazers connection to third parties in
# July 2026, so a history collected before the restriction is the only copy
# that will ever exist and must survive later scans.
# ---------------------------------------------------------------------------
def _history(days, collected_at=None, total=500):
    return StarHistory(
        total_stars=total,
        collected=sum(d[1] for d in days),
        complete=True,
        days=[StarDay(date=d, count=c) for d, c in days],
        collected_at=collected_at,
    )


def test_live_collection_is_never_replaced_by_the_prior_history():
    live = _history([("2026-07-22", 5)], collected_at="2026-07-22")
    prior = _history([("2026-07-01", 99)], collected_at="2026-07-01")
    warnings = []
    assert _carry_forward_star_history(live, prior, 500, warnings) is live
    assert warnings == []


def test_prior_history_is_kept_when_collection_fails():
    prior = _history([("2026-07-01", 99)], collected_at="2026-07-01", total=400)
    warnings = []
    carried = _carry_forward_star_history(None, prior, 500, warnings)
    assert carried is not None
    assert [d.date for d in carried.days] == ["2026-07-01"]
    assert carried.collected_at == "2026-07-01"
    # The snapshot describes the repository on the day it was captured; today's
    # star count must not be grafted onto it as growth nobody observed.
    assert carried.total_stars == 400
    assert len(warnings) == 1 and "carried forward from 2026-07-01" in warnings[0]


def test_carrying_forward_does_not_alias_the_prior_history():
    prior = _history([("2026-07-01", 99)], collected_at="2026-07-01")
    carried = _carry_forward_star_history(None, prior, 500, [])
    carried.days[0].count = 1
    assert prior.days[0].count == 99


def test_a_history_captured_before_dates_were_recorded_falls_back_to_its_last_day():
    prior = _history([("2026-06-30", 4), ("2026-07-02", 7)])
    carried = _carry_forward_star_history(None, prior, 500, [])
    assert carried.collected_at == "2026-07-02"


def test_nothing_to_carry_forward_stays_absent():
    assert _carry_forward_star_history(None, None, 500, []) is None
    assert _carry_forward_star_history(None, _history([]), 500, []) is None


def test_live_collection_stamps_the_day_it_ran():
    gh = FakeClient(pages=[_page(["2026-07-22T10:00:00Z"], False)])
    sh = _star_history(gh, "o", "n", 1, [])
    assert sh is not None and sh.collected_at == datetime.now(timezone.utc).date().isoformat()
