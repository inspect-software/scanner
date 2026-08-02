"""Abandonment: the evidence tiers, the guards, and the overall multiplier.

The tests that matter most here are the negative ones. Anything can flag a
repository that has not been touched in three years; the question is whether a
finished library, a project whose maintainer answers without committing, and a
repository nobody has made a request of all come back clean.
"""

from datetime import datetime, timedelta, timezone

from scanner.abandonment import (
    DROUGHT_HARD_DAYS,
    DROUGHT_SOFT_DAYS,
    ROTTEN_ISSUES_MIN,
    STATE_CAP,
    UNANSWERED_PRS_MIN,
    assess,
)
from scanner.metrics import compute_metrics, metric_abandonment
from scanner.models import (
    Activity,
    AdvisoryFinding,
    DependencyAdvisories,
    CommitRecord,
    ContributionFlow,
    Contributor,
    DependencySignals,
    EcosystemData,
    EcosystemPackage,
    IssueMetrics,
    Maintainership,
    Popularity,
    RepoData,
    RepoInfo,
    TrackedItem,
)


def ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def commits(*, human_days_ago: int | None, count: int = 30, bot_span_days: int = 900):
    """A sampled window: optionally one human commit, the rest automation.

    The automation spans years by default, which is what a repository kept
    alive by Dependabot alone actually looks like — its newest commit is days
    old and every one of them is a version bump.
    """
    step = max(1, bot_span_days // count)
    records = [
        CommitRecord(
            oid=f"bot{i}",
            committed_at=ago(2 + i * step),
            headline="chore(deps): bump left-pad",
            author_login="dependabot[bot]",
            is_bot=True,
        )
        for i in range(count)
    ]
    if human_days_ago is not None:
        records.append(
            CommitRecord(
                oid="human",
                committed_at=ago(human_days_ago),
                headline="Fix the thing",
                author_login="maintainer",
            )
        )
    return sorted(records, key=lambda c: c.committed_at, reverse=True)


def items(count: int, *, age_days: int, replier: str | None = None) -> list[TrackedItem]:
    return [
        TrackedItem(
            number=100 + i,
            created_at=ago(age_days),
            last_comment_at=ago(age_days - 1) if replier else None,
            last_comment_author=replier,
        )
        for i in range(count)
    ]


def repo(
    *,
    human_days_ago: int | None = 800,
    archived: bool = False,
    open_prs: list[TrackedItem] | None = None,
    open_issues: list[TrackedItem] | None = None,
    open_issue_count: int = 40,
    last_merged_pr_days: int | None = 900,
    collected: bool = True,
    packages: list[EcosystemPackage] | None = None,
    advisories: DependencyAdvisories | None = None,
    days_since_release: int | None = 1200,
    mean_release_gap: float | None = 30.0,
) -> RepoData:
    return RepoData(
        repo=RepoInfo(is_archived=archived, created_at=ago(3000)),
        popularity=Popularity(stars=5000),
        activity=Activity(
            recent_commits=commits(human_days_ago=human_days_ago),
            commits_last_year=120,
            active_weeks_last_year=30,
            days_since_last_push=human_days_ago if human_days_ago else 2,
            releases_count=12,
            days_since_latest_release=days_since_release,
            mean_days_between_releases=mean_release_gap,
        ),
        contribution_flow=ContributionFlow(
            collected=collected,
            last_merged_pr_at=ago(last_merged_pr_days) if last_merged_pr_days else None,
            oldest_open_prs=open_prs if open_prs is not None else items(6, age_days=700),
            oldest_open_issues=open_issues if open_issues is not None else items(8, age_days=900),
        ),
        maintainership=Maintainership(
            bus_factor=3,
            top_contributors=[Contributor(login="maintainer", commits=500)],
            issues=IssueMetrics(open_issues=open_issue_count),
        ),
        ecosystem=EcosystemData(packages=packages or []),
        dependencies=DependencySignals(advisories=advisories or DependencyAdvisories()),
    )


# --- Declared -------------------------------------------------------------


def test_archived_repository_is_declared_without_any_inference():
    result = assess(repo(archived=True, human_days_ago=1))
    assert result.state == "declared"
    assert result.declared_reason == "archived"
    assert result.cap == STATE_CAP["declared"]


def test_every_published_package_deprecated_is_declared():
    result = assess(
        repo(packages=[EcosystemPackage(ecosystem="npm", name="left-pad",
                         registry_url="https://npmjs.com/package/left-pad",
                         matches_repo=True, is_deprecated=True)])
    )
    assert result.state == "declared"
    assert result.declared_reason == "packages_deprecated"


def test_one_deprecated_package_among_several_is_not_a_declaration():
    """Retiring a package is not retiring the project that publishes it."""
    result = assess(
        repo(
            packages=[
                EcosystemPackage(ecosystem="npm", name="old",
                                 registry_url="https://npmjs.com/package/old",
                                 matches_repo=True, is_deprecated=True),
                EcosystemPackage(ecosystem="npm", name="current",
                                 registry_url="https://npmjs.com/package/current",
                                 matches_repo=True),
            ]
        )
    )
    assert result.declared_reason is None


# --- Drought --------------------------------------------------------------


def test_recent_human_commit_is_maintained():
    assert assess(repo(human_days_ago=10)).state == "maintained"


def test_automation_alone_does_not_count_as_a_human_commit():
    """The whole sampled window is Dependabot; the push recency is two days."""
    result = assess(repo(human_days_ago=None))
    assert result.state != "maintained"
    assert result.drought_is_floor is True


def test_no_commit_sample_is_unverified():
    data = repo()
    data.activity.recent_commits = []
    result = assess(data)
    assert result.state == "unverified"
    assert result.unverified_reason == "no_commit_sample"


def test_unread_queues_stop_at_unverified():
    """Three of the seven signals read the tracker; without it, no verdict."""
    result = assess(repo(collected=False))
    assert result.state == "unverified"
    assert result.unverified_reason == "queues_not_read"


def test_young_repository_is_unverified():
    data = repo(human_days_ago=200)
    data.repo.created_at = ago(60)
    assert assess(data).unverified_reason == "repository_too_young"


# --- Obligations ----------------------------------------------------------


def test_drought_with_three_unmet_obligations_is_likely_abandoned():
    result = assess(repo())
    assert result.state == "likely_abandoned"
    assert len(result.signals) >= 3
    assert "unanswered_contributions" in result.signals
    assert "issue_rot" in result.signals
    assert result.cap == 34


def test_unanswered_contributions_needs_both_a_queue_and_no_merges():
    """Old open pull requests beside recent merges are a backlog, not neglect."""
    result = assess(repo(last_merged_pr_days=30))
    assert "unanswered_contributions" not in result.signals


def test_issues_a_maintainer_answered_are_not_rot():
    result = assess(
        repo(open_issues=items(ROTTEN_ISSUES_MIN + 3, age_days=900, replier="maintainer"))
    )
    assert "issue_rot" not in result.signals


def test_unfixed_direct_advisory_is_an_obligation():
    advisories = DependencyAdvisories(
        collected=True,
        assessed_count=10,
        affected_count=1,
        findings=[
            AdvisoryFinding(
                ecosystem="npm",
                name="lodash",
                version="4.17.11",
                direct=True,
                severity="high",
                fixed_version="4.17.21",
                oldest_advisory_days=1500,
                advisory_count=1,
            )
        ],
    )
    assert "unfixed_advisory" in assess(repo(advisories=advisories)).signals


# --- Guards ---------------------------------------------------------------


def test_a_maintainer_still_replying_holds_the_result_at_dormant():
    """Answering an issue is maintenance, even with no commits behind it."""
    result = assess(
        repo(
            open_issues=items(8, age_days=900, replier="maintainer"),
            open_prs=[
                TrackedItem(
                    number=1,
                    created_at=ago(700),
                    last_comment_at=ago(20),
                    last_comment_author="maintainer",
                )
            ],
        )
    )
    assert "maintainer_replying" in result.guards
    assert result.state == "dormant"
    assert result.factor == 100


def test_a_finished_library_with_nothing_asked_of_it_is_dormant():
    """Quiet for years, no open requests, dependencies clean: complete, not dead."""
    result = assess(
        repo(
            open_prs=[],
            open_issues=[],
            open_issue_count=0,
            last_merged_pr_days=None,
            days_since_release=None,
            advisories=DependencyAdvisories(collected=True, assessed_count=12, affected_count=0),
        )
    )
    assert {"no_open_demand", "dependencies_clean"} <= set(result.guards)
    assert result.state == "dormant"
    assert result.factor == 100


def test_dormant_carries_no_penalty_at_all():
    result = assess(repo(open_prs=[], open_issues=[], open_issue_count=1))
    assert result.state == "dormant"
    assert result.factor == 100
    assert result.cap is None


# --- Metric and policy ----------------------------------------------------


def test_metric_reports_the_state_and_the_multiplier():
    metric = metric_abandonment(repo())
    assert metric is not None
    assert metric.inputs["red_flag"] is True
    assert metric.inputs["state"] == "likely_abandoned"
    assert metric.value == metric.inputs["multiplier_pct"] == 60


def test_maintained_repository_flies_no_flag():
    metric = metric_abandonment(repo(human_days_ago=5))
    assert metric is not None
    assert metric.inputs["red_flag"] is False
    assert metric.value == 100


def test_policy_multiplies_the_overall_score_and_applies_the_ceiling():
    metrics = compute_metrics(repo())
    overall = metrics.overall
    assert overall is not None
    before = overall.inputs["weighted_overall_before_abandonment"]
    assert overall.inputs["abandonment_state"] == "likely_abandoned"
    assert overall.inputs["abandonment_multiplier"] == 60
    assert overall.value == min(max(1, round(before * 60 / 100)), 49)
    assert "abandonment_overall_adjustment" in [n.code for n in overall.notes]


def test_abandonment_carries_no_additive_weight_in_vitality():
    """The flag must not lift a maintained project's Vitality rollup."""
    clean = compute_metrics(repo(human_days_ago=5))
    vitality = next(c for c in clean.categories if c.key == "vitality")
    scored = {m.key: m.value for m in vitality.metrics}
    assert "abandonment" in scored
    expected = round(
        (scored["development_activity"] * 0.6 + scored["release_discipline"] * 0.4) / 1.0
    )
    assert vitality.value == expected


def test_a_squatted_package_cannot_declare_a_project_abandoned():
    """PyPI `ComfyUI`: a 0.0.1 placeholder that declares no repository.

    Measured against the live registry — under a `matches_repo is not False`
    test this took comfyanonymous/ComfyUI, one of the most actively developed
    projects in its field, from 68 to 27.
    """
    result = assess(
        repo(
            human_days_ago=3,
            packages=[
                EcosystemPackage(
                    ecosystem="pypi",
                    name="ComfyUI",
                    registry_url="https://pypi.org/project/ComfyUI/",
                    matches_repo=None,
                    latest_version_yanked=True,
                )
            ],
        )
    )
    assert result.declared_reason is None
    assert result.state == "maintained"


def test_a_yanked_latest_version_is_not_a_declaration():
    """A withdrawn release is a bad build, not the end of a project."""
    result = assess(
        repo(
            human_days_ago=3,
            packages=[
                EcosystemPackage(
                    ecosystem="npm",
                    name="thing",
                    registry_url="https://npmjs.com/package/thing",
                    matches_repo=True,
                    latest_version_yanked=True,
                )
            ],
        )
    )
    assert result.declared_reason is None
    assert result.state == "maintained"
