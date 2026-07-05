from scanner.metrics import (
    METRICS_VERSION,
    band_for,
    compute_metrics,
    metric_activity,
    metric_maintainer_resilience,
    metric_responsiveness,
    metric_security_posture,
)
from scanner.models import (
    Activity,
    CommunityHealth,
    DependencySignals,
    IssueMetrics,
    Maintainership,
    QualitySignals,
    RepoData,
    SecuritySignals,
)


def test_band_boundaries():
    assert band_for(1) == "critical"
    assert band_for(29) == "critical"
    assert band_for(30) == "at_risk"
    assert band_for(49) == "at_risk"
    assert band_for(50) == "moderate"
    assert band_for(69) == "moderate"
    assert band_for(70) == "good"
    assert band_for(84) == "good"
    assert band_for(85) == "excellent"
    assert band_for(100) == "excellent"


def test_activity_healthy_project():
    data = RepoData(
        activity=Activity(
            days_since_last_push=2,
            active_weeks_last_year=50,
            commits_last_year=400,
            releases_count=30,
            mean_days_between_releases=14.0,
        )
    )
    m = metric_activity(data)
    assert m is not None
    assert m.band in ("good", "excellent")
    assert m.note is None


def test_activity_abandoned_project():
    data = RepoData(
        activity=Activity(
            days_since_last_push=800,
            active_weeks_last_year=0,
            commits_last_year=0,
            releases_count=0,
        )
    )
    m = metric_activity(data)
    assert m is not None
    assert m.value <= 5
    assert m.band == "critical"


def test_activity_missing_stats_renormalizes():
    # Only push recency known — score should still compute, with a note.
    data = RepoData(activity=Activity(days_since_last_push=3, releases_count=None))
    m = metric_activity(data)
    assert m is not None
    assert m.note is not None


def test_activity_no_data_is_none():
    assert metric_activity(RepoData()) is None


def test_bus_factor_one_scores_low():
    data = RepoData(
        maintainership=Maintainership(
            bus_factor=1, top_contributor_share=0.97, contributors_sampled=4
        )
    )
    m = metric_maintainer_resilience(data)
    assert m is not None
    assert m.band in ("critical", "at_risk")


def test_bus_factor_high_scores_high():
    data = RepoData(
        maintainership=Maintainership(
            bus_factor=12, top_contributor_share=0.15, contributors_sampled=100
        )
    )
    m = metric_maintainer_resilience(data)
    assert m is not None
    assert m.band in ("good", "excellent")


def test_responsiveness_requires_some_data():
    assert metric_responsiveness(RepoData()) is None
    data = RepoData(
        maintainership=Maintainership(
            issues=IssueMetrics(closed_ratio=0.9, merged_prs=90, closed_unmerged_prs=10)
        )
    )
    m = metric_responsiveness(data)
    assert m is not None
    assert m.value >= 85


def test_security_lockfile_component_skipped_without_manifests():
    signals = SecuritySignals(
        has_security_policy=True, has_dependabot_config=True, has_codeql_workflow=True
    )
    with_manifests = RepoData(
        security_signals=signals,
        dependencies=DependencySignals(manifests=["package.json"]),
    )
    without_manifests = RepoData(security_signals=signals)
    scored_with = metric_security_posture(with_manifests)
    scored_without = metric_security_posture(without_manifests)
    # No manifests -> lockfile pinning not applicable -> perfect score possible
    assert scored_without.value == 100
    # Manifests but no lockfile -> penalized
    assert scored_with.value < 100


def test_overall_present_and_versioned():
    data = RepoData(
        activity=Activity(days_since_last_push=1, active_weeks_last_year=40,
                          commits_last_year=200, releases_count=10,
                          mean_days_between_releases=30.0),
        maintainership=Maintainership(bus_factor=3, top_contributor_share=0.4,
                                      contributors_sampled=20,
                                      issues=IssueMetrics(closed_ratio=0.8)),
        community=CommunityHealth(has_readme=True, has_license=True),
        quality_signals=QualitySignals(has_ci=True, has_tests=True),
    )
    metrics = compute_metrics(data)
    assert metrics.metrics_version == METRICS_VERSION
    assert metrics.overall is not None
    assert 1 <= metrics.overall.value <= 100
    assert metrics.overall.band == band_for(metrics.overall.value)


def test_overall_none_when_nothing_computable():
    metrics = compute_metrics(RepoData())
    # community/practices/security always compute (checklists), so overall exists
    assert metrics.overall is not None
    assert metrics.activity is None
    assert metrics.maintainer_resilience is None
    assert metrics.responsiveness is None
    assert "renormalized" in (metrics.overall.note or "")
