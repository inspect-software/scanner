"""Bots are excluded from maintainer figures (bus factor, share, breadth)."""

from __future__ import annotations

from scanner.collect import _maintainership
from scanner.snapshot import IssueCounts


class FakeClient:
    """Serves one contributor list; everything else is absent."""

    token = "tok"

    def __init__(self, contributors):
        self._contributors = contributors

    def get_optional(self, path, params=None, timeout=None):
        return self._contributors if path.endswith("/contributors") else None

    def get(self, path, params=None, timeout=None):
        return None

    def graphql(self, query, variables):
        return {}


def contributor(login, commits, type_="User"):
    return {"login": login, "contributions": commits, "type": type_}


COUNTS = IssueCounts(open_issues=1, closed_issues=1, open_prs=0, merged_prs=0, closed_prs=0)


def collect(contributors):
    return _maintainership(FakeClient(contributors), "/repos/o/n", "o", "n", COUNTS, [])


def test_bots_do_not_count_as_maintainers():
    # Modelled on prettier: two of the top three contributors are release bots.
    result = collect([
        contributor("fisker", 2879),
        contributor("renovate[bot]", 1922, "Bot"),
        contributor("dependabot[bot]", 1578, "Bot"),
        contributor("vjeux", 573),
        contributor("sosukesuzuki", 504),
    ])
    assert result.bot_contributors == 2
    assert result.contributors_sampled == 3
    assert [c.login for c in result.top_contributors] == ["fisker", "vjeux", "sosukesuzuki"]
    # Share is over human commits only: 2879 / (2879 + 573 + 504).
    assert result.top_contributor_share == round(2879 / 3956, 3)
    assert result.bus_factor == 1


def test_bot_login_suffix_without_the_bot_type():
    # Some payloads type an app account as User; the reserved suffix backstops.
    result = collect([contributor("someone", 10), contributor("ci-helper[bot]", 90)])
    assert result.bot_contributors == 1
    assert result.bus_factor == 1
    assert result.top_contributor_share == 1.0


def test_repository_maintained_only_by_automation():
    result = collect([contributor("renovate[bot]", 500, "Bot")])
    assert result.bot_contributors == 1
    assert result.contributors_sampled is None
    assert result.bus_factor is None
    assert result.top_contributor_share is None


def test_human_only_repository_is_unchanged():
    result = collect([contributor("a", 60), contributor("b", 40)])
    assert result.bot_contributors == 0
    assert result.contributors_sampled == 2
    assert result.bus_factor == 1
