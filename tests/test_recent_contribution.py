"""Windowed pull-request outcomes, newcomer classification, README badges."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scanner.collect import _readme_badges, _recent_prs
from scanner.github import GitHubError
from scanner.metrics import _newcomer_component, metric_responsiveness
from scanner.models import (
    ContributionFlow,
    IssueMetrics,
    Maintainership,
    RecentPullRequests,
    RepoData,
    RepoInfo,
)
from scanner.readme import scan_readme
from scanner.snapshot import MAX_DECIDED_PR_SAMPLE, RepoSnapshot, probe_author_merge_counts

NOW = datetime.now(timezone.utc)


def _pr(days_ago: float, merged: bool, author: str | None, author_type: str = "User") -> dict:
    return {
        "number": int(days_ago * 100),
        "decided_at": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
        "merged": merged,
        "author": author,
        "author_type": author_type,
    }


class FakeGitHub:
    """Stands in for GitHubClient, recording the author probe it was asked for."""

    def __init__(self, counts: dict[str, int] | None = None, error: str | None = None):
        self._counts = counts or {}
        self._error = error
        self.queries: list[str] = []

    def graphql(self, query: str, variables: dict) -> dict:
        if self._error:
            raise GitHubError(self._error)
        data = {}
        for key, value in variables.items():
            self.queries.append(value)
            login = value.rsplit("author:", 1)[1]
            if login in self._counts:
                data[f"a{key[1:]}"] = {"issueCount": self._counts[login]}
        return data


# --- windowing -------------------------------------------------------------


def test_windows_split_at_seven_and_thirty_days():
    sample = [
        _pr(1, True, "amy"),
        _pr(3, False, "ben"),
        _pr(20, True, "cal"),
        _pr(45, True, "dee"),
    ]
    recent = _recent_prs(FakeGitHub({"amy": 1, "ben": 0, "cal": 9}), "o", "r", sample, [])

    assert (recent.decided_7d, recent.merged_7d) == (2, 1)
    assert (recent.decided_30d, recent.merged_30d) == (3, 2)


def test_bot_pull_requests_are_excluded_and_counted():
    sample = [
        _pr(2, True, "dependabot[bot]", author_type="Bot"),
        _pr(3, True, "renovate[bot]", author_type="User"),  # login suffix still catches it
        _pr(4, True, "amy"),
        _pr(50, True, "otherbot[bot]", author_type="Bot"),  # outside the window
    ]
    recent = _recent_prs(FakeGitHub({"amy": 1}), "o", "r", sample, [])

    assert recent.decided_30d == 1
    assert recent.bot_prs_excluded_30d == 2


def test_sample_exhausted_only_when_the_page_ends_inside_the_window():
    full_and_recent = [_pr(i * 0.1, True, f"u{i}") for i in range(MAX_DECIDED_PR_SAMPLE)]
    recent = _recent_prs(FakeGitHub(), "o", "r", full_and_recent, [])
    assert recent.sample_exhausted is True

    reaching_back = full_and_recent[:-1] + [_pr(90, True, "old")]
    recent = _recent_prs(FakeGitHub(), "o", "r", reaching_back, [])
    assert recent.sample_exhausted is False


def test_short_sample_is_never_exhausted():
    recent = _recent_prs(FakeGitHub({"amy": 1}), "o", "r", [_pr(1, True, "amy")], [])
    assert recent.sample_exhausted is False


# --- newcomer classification ----------------------------------------------


def test_author_whose_only_merge_is_in_the_window_is_a_newcomer():
    sample = [_pr(2, True, "amy")]
    recent = _recent_prs(FakeGitHub({"amy": 1}), "o", "r", sample, [])

    assert recent.newcomer_authors_30d == 1
    assert (recent.newcomer_decided_30d, recent.newcomer_merged_30d) == (1, 1)


def test_author_with_prior_merges_is_not_a_newcomer():
    sample = [_pr(2, True, "amy")]
    recent = _recent_prs(FakeGitHub({"amy": 40}), "o", "r", sample, [])

    assert recent.newcomer_authors_30d == 0
    assert recent.newcomer_decided_30d == 0


def test_rejected_first_attempt_counts_as_a_decided_newcomer_pr():
    """Someone with no merges at all who was turned away is the case the
    component exists to catch — it must not vanish from the denominator."""
    sample = [_pr(2, False, "amy")]
    recent = _recent_prs(FakeGitHub({"amy": 0}), "o", "r", sample, [])

    assert (recent.newcomer_decided_30d, recent.newcomer_merged_30d) == (1, 0)


def test_newcomer_with_several_merges_in_one_window_still_counts_as_new():
    sample = [_pr(2, True, "amy"), _pr(5, True, "amy"), _pr(6, False, "amy")]
    recent = _recent_prs(FakeGitHub({"amy": 2}), "o", "r", sample, [])

    assert recent.newcomer_authors_30d == 1
    assert (recent.newcomer_decided_30d, recent.newcomer_merged_30d) == (3, 2)


def test_failed_probe_leaves_newcomer_figures_unset_rather_than_zero():
    warnings: list[str] = []
    recent = _recent_prs(
        FakeGitHub(error="rate limited"), "o", "r", [_pr(2, True, "amy")], warnings
    )

    assert recent.newcomer_decided_30d is None
    assert recent.decided_30d == 1
    assert any("First-time contributor lookup failed" in w for w in warnings)


def test_unprobed_authors_are_excluded_and_the_shortfall_is_warned():
    warnings: list[str] = []
    sample = [_pr(2, True, "amy"), _pr(3, True, "ben")]
    # Only amy's history came back.
    recent = _recent_prs(FakeGitHub({"amy": 1}), "o", "r", sample, warnings)

    assert (recent.authors_30d, recent.authors_probed_30d) == (2, 1)
    assert recent.newcomer_authors_30d == 1
    assert any("1 of 2 authors" in w for w in warnings)


def test_no_authors_means_zero_newcomers_not_unknown():
    recent = _recent_prs(FakeGitHub(), "o", "r", [], [])
    assert recent.newcomer_decided_30d == 0
    assert recent.authors_30d == 0


def test_probe_skips_logins_that_did_not_come_from_github():
    gh = FakeGitHub({"amy": 1})
    probe_author_merge_counts(gh, "o", "r", ["amy", "not a login", "x" * 60])

    assert gh.queries == ["repo:o/r type:pr is:merged author:amy"]


# --- scoring ---------------------------------------------------------------


def test_component_scores_the_newcomers_own_acceptance_rate():
    component = _newcomer_component(
        RecentPullRequests(newcomer_decided_30d=4, newcomer_merged_30d=3)
    )
    assert component.points == pytest.approx(13 * 3 / 4, abs=0.1)
    assert component.status == "partial"


def test_component_is_excluded_when_no_newcomer_pr_was_decided():
    """Nobody knocking is not the project's doing, so it must not score zero —
    it must drop out and let the metric's other weights renormalize."""
    component = _newcomer_component(
        RecentPullRequests(newcomer_decided_30d=0, newcomer_merged_30d=0)
    )
    assert component.status == "excluded"


def test_component_is_excluded_when_the_probe_never_ran():
    assert _newcomer_component(RecentPullRequests()).status == "excluded"


def _repo_data(recent: RecentPullRequests) -> RepoData:
    return RepoData(
        repo=RepoInfo(full_name="o/r", url="https://github.com/o/r"),
        maintainership=Maintainership(
            issues=IssueMetrics(
                open_issues=1, closed_issues=9, closed_ratio=0.9,
                merged_prs=90, closed_unmerged_prs=10,
            )
        ),
        contribution_flow=ContributionFlow(collected=True, recent_prs=recent),
    )


def test_a_closed_door_scores_below_an_open_one_on_the_same_all_time_record():
    """Identical all-time acceptance, opposite treatment of first-timers."""
    welcoming = metric_responsiveness(
        _repo_data(RecentPullRequests(newcomer_decided_30d=5, newcomer_merged_30d=5))
    )
    closed = metric_responsiveness(
        _repo_data(RecentPullRequests(newcomer_decided_30d=5, newcomer_merged_30d=0))
    )

    assert welcoming.value > closed.value


def test_reports_predating_collection_renormalize_instead_of_losing_points():
    """Every stored report lacks these fields until it is rescanned. The
    component must then drop out and let the remaining weights carry the
    metric, rather than scoring as a repository that turned newcomers away."""
    absent = metric_responsiveness(_repo_data(RecentPullRequests()))
    hostile = metric_responsiveness(
        _repo_data(RecentPullRequests(newcomer_decided_30d=5, newcomer_merged_30d=0))
    )
    welcoming = metric_responsiveness(
        _repo_data(RecentPullRequests(newcomer_decided_30d=5, newcomer_merged_30d=5))
    )

    assert hostile.value < absent.value < welcoming.value
    # Renormalized onto the surviving components: the metric is exactly the
    # 90% the issue and pull-request rates alone earn.
    assert absent.value == 90
    assert "renormalized" in (absent.note or "")


# --- README badges ---------------------------------------------------------


def test_markdown_html_and_rst_badges_are_all_found():
    text = """# proj
[![CI](https://github.com/o/r/actions/workflows/ci.yml/badge.svg)](https://x)
<img src="https://img.shields.io/npm/v/pkg.svg" alt="npm">
.. image:: https://codecov.io/gh/o/r/badge.svg
"""
    found = scan_readme(text)
    assert found.total == 3
    assert found.hosts == ["codecov.io", "github.com", "shields.io"]


def test_non_badge_images_are_ignored():
    found = scan_readme("![logo](docs/logo.png)\n![shot](https://example.com/a.svg)")
    assert found.total == 0


def test_repeated_badge_urls_count_once():
    url = "https://img.shields.io/npm/v/pkg.svg"
    assert scan_readme(f"![a]({url})\n![b]({url})").total == 1


def test_header_badges_stop_at_the_first_section_heading():
    text = """# proj
![a](https://img.shields.io/a.svg)

## Install
![b](https://badgen.net/b)
"""
    found = scan_readme(text)
    assert (found.total, found.header) == (2, 1)


def test_our_own_badge_is_recognized_in_every_form_handed_out():
    for url in (
        "https://inspect.software/badge/v1/owner/repo.svg",
        "https://osaudit.org/badge/v1/owner/repo.svg",
        # The published copy: somebody else's host, so it is matched on the
        # scanned repository's own name inside the badges-repo layout.
        "https://raw.githubusercontent.com/acme/badges/main/v1/o/owner/repo.svg",
    ):
        assert scan_readme(f"![b]({url})", "owner/repo").has_inspect_badge, url


def test_another_projects_published_badge_is_not_read_as_ours():
    url = "https://raw.githubusercontent.com/acme/badges/main/v1/o/other/project.svg"
    assert scan_readme(f"![b]({url})", "owner/repo").has_inspect_badge is False


def test_protocol_relative_badge_urls_are_matched():
    assert scan_readme("![b](//img.shields.io/npm/v/pkg.svg)").total == 1


def test_rest_fallback_records_not_collected_rather_than_zero_badges():
    """No README read and a README with no badges must stay distinguishable."""
    assert _readme_badges(RepoSnapshot(repo={}, readme=None)).collected is False
    assert _readme_badges(RepoSnapshot(repo={}, readme="# proj\n")).collected is True
