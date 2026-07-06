from scanner.metrics import (
    METRICS_VERSION,
    REPO_CATEGORIES,
    band_for,
    compute_metrics,
    metric_development_activity,
    metric_documentation,
    metric_maintainer_resilience,
    metric_popularity,
    metric_release_discipline,
    metric_responsiveness,
    metric_security_posture,
    metric_stewardship,
)
from scanner.models import (
    Activity,
    CommunityHealth,
    DependencySignals,
    IssueMetrics,
    Maintainership,
    OwnerProfile,
    Popularity,
    QualitySignals,
    RepoData,
    RepoInfo,
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


def test_category_weights_sum_to_one():
    assert abs(sum(c.weight for c in REPO_CATEGORIES) - 1.0) < 1e-9
    # within each category, metric weights sum to 1.0
    for cat in REPO_CATEGORIES:
        assert abs(sum(w for w, _ in cat.metrics.values()) - 1.0) < 1e-9


# --- development activity -----------------------------------------------------


def test_development_activity_healthy():
    data = RepoData(activity=Activity(days_since_last_push=2, active_weeks_last_year=50,
                                      commits_last_year=400))
    m = metric_development_activity(data)
    assert m.band in ("good", "excellent")
    assert m.note is None


def test_development_activity_abandoned():
    data = RepoData(activity=Activity(days_since_last_push=800, active_weeks_last_year=0,
                                      commits_last_year=0))
    m = metric_development_activity(data)
    assert m.value <= 5 and m.band == "critical"


def test_development_activity_no_data_is_none():
    assert metric_development_activity(RepoData()) is None


# --- release discipline (new) -------------------------------------------------


def test_release_discipline_none_when_unknown():
    assert metric_release_discipline(RepoData()) is None


def test_release_discipline_no_releases_missed():
    m = metric_release_discipline(RepoData(activity=Activity(releases_count=0)))
    assert m.value <= 3
    assert all(c.status == "missed" for c in m.components)


def test_release_discipline_healthy():
    data = RepoData(activity=Activity(releases_count=30, days_since_latest_release=20,
                                      mean_days_between_releases=14.0))
    m = metric_release_discipline(data)
    assert m.band in ("good", "excellent")
    by = {c.name: c for c in m.components}
    assert by["Ships releases"].status == "met"
    assert by["Release recency"].status == "met"


# --- popularity (new) ---------------------------------------------------------


def test_popularity_log_scaled():
    small = metric_popularity(RepoData(popularity=Popularity(stars=5)))
    big = metric_popularity(RepoData(popularity=Popularity(stars=5000, forks=1000, watchers=500)))
    assert small.value < big.value
    assert big.value >= 85


# --- stewardship (new): the ownership influence -------------------------------


def test_stewardship_none_without_owner():
    assert metric_stewardship(RepoData()) is None


def test_stewardship_org_beats_equivalent_user():
    org = RepoData(owner=OwnerProfile(login="acme", type="Organization", is_verified=True,
                                      followers=2000, public_repos=40, account_age_days=2500))
    user = RepoData(owner=OwnerProfile(login="joe", type="User",
                                       followers=2000, public_repos=40, account_age_days=2500))
    mo, mu = metric_stewardship(org), metric_stewardship(user)
    assert mo.value > mu.value
    by_org = {c.name: c for c in mo.components}
    by_user = {c.name: c for c in mu.components}
    # ownership backing rewards the organization
    assert by_org["Ownership backing"].status == "met"
    assert by_user["Ownership backing"].status == "partial"
    # verified domain is not applicable to a user account
    assert by_user["Verified domain"].status == "excluded"


def test_stewardship_unverified_org_below_verified():
    verified = metric_stewardship(RepoData(owner=OwnerProfile(
        login="a", type="Organization", is_verified=True, followers=500, public_repos=20)))
    unverified = metric_stewardship(RepoData(owner=OwnerProfile(
        login="b", type="Organization", is_verified=False, followers=500, public_repos=20)))
    assert verified.value > unverified.value


# --- documentation (new) ------------------------------------------------------


def test_documentation_components():
    data = RepoData(
        repo=RepoInfo(homepage="https://x.io", topics=["a", "b"], has_wiki=True),
        community=CommunityHealth(has_readme=True, has_description=True),
        quality_signals=QualitySignals(has_docs_dir=True),
    )
    m = metric_documentation(data)
    by = {c.name: c for c in m.components}
    assert by["README"].status == "met"
    assert by["Documentation directory"].status == "met"
    assert by["Documentation / homepage site"].status == "met"
    assert by["Topics"].status == "met"
    assert m.value == 100


# --- unchanged metrics --------------------------------------------------------


def test_bus_factor_one_scores_low():
    data = RepoData(maintainership=Maintainership(bus_factor=1, top_contributor_share=0.97,
                                                  contributors_sampled=4))
    assert metric_maintainer_resilience(data).band in ("critical", "at_risk")


def test_bus_factor_high_scores_high():
    data = RepoData(maintainership=Maintainership(bus_factor=12, top_contributor_share=0.15,
                                                  contributors_sampled=100))
    assert metric_maintainer_resilience(data).band in ("good", "excellent")


def test_responsiveness_requires_some_data():
    assert metric_responsiveness(RepoData()) is None
    data = RepoData(maintainership=Maintainership(
        issues=IssueMetrics(closed_ratio=0.9, merged_prs=90, closed_unmerged_prs=10)))
    assert metric_responsiveness(data).value >= 85


def test_security_lockfile_excluded_without_manifests():
    signals = SecuritySignals(has_security_policy=True, has_dependabot_config=True,
                              has_codeql_workflow=True)
    scored_without = metric_security_posture(RepoData(security_signals=signals))
    scored_with = metric_security_posture(RepoData(
        security_signals=signals, dependencies=DependencySignals(manifests=["package.json"])))
    assert scored_without.value == 100
    assert scored_with.value < 100
    by = {c.name: c for c in scored_without.components}
    assert by["Dependency lockfiles"].status == "excluded"


# --- category rollup & overall ------------------------------------------------


def test_categories_and_rollups():
    data = RepoData(
        owner=OwnerProfile(login="acme", type="Organization", is_verified=True,
                           followers=1000, public_repos=30, account_age_days=2000),
        repo=RepoInfo(homepage="https://x.io", topics=["a"], has_wiki=True),
        popularity=Popularity(stars=3000, forks=400, watchers=100),
        activity=Activity(days_since_last_push=1, active_weeks_last_year=45, commits_last_year=300,
                          releases_count=20, days_since_latest_release=15, mean_days_between_releases=20.0),
        maintainership=Maintainership(bus_factor=4, top_contributor_share=0.3, contributors_sampled=30,
                                      issues=IssueMetrics(closed_ratio=0.85, merged_prs=90,
                                                          closed_unmerged_prs=5)),
        community=CommunityHealth(has_readme=True, has_license=True, has_contributing=True,
                                  has_description=True),
        quality_signals=QualitySignals(has_ci=True, has_tests=True, has_docs_dir=True,
                                        has_linter_config=True),
    )
    metrics = compute_metrics(data)
    assert metrics.metrics_version == METRICS_VERSION
    assert metrics.overall is not None
    assert metrics.overall.band == band_for(metrics.overall.value)
    keys = {c.key for c in metrics.categories}
    assert keys == {"vitality", "community", "governance", "engineering", "security"}
    for cat in metrics.categories:
        assert cat.value is not None
        assert cat.band == band_for(cat.value)
    # helper lookup works
    assert metrics.by_key("stewardship") is not None
    assert metrics.category("vitality").value == metrics.category("vitality").value


def test_overall_is_weighted_mean_of_categories():
    metrics = compute_metrics(RepoData(
        popularity=Popularity(stars=100),
        community=CommunityHealth(has_readme=True),
    ))
    cats = {c.key: c for c in metrics.categories}
    total_w = sum(c.weight for c in cats.values())
    expected = round(sum(c.value * c.weight for c in cats.values()) / total_w)
    assert metrics.overall.value == max(1, min(100, expected))


def test_empty_data_drops_categories_without_data():
    metrics = compute_metrics(RepoData())
    keys = {c.key for c in metrics.categories}
    # vitality (activity/releases) and governance (bus factor/responsiveness/steward)
    # have no data -> dropped; community/engineering/security are checklists -> present
    assert "vitality" not in keys
    assert "governance" not in keys
    assert {"community", "engineering", "security"} <= keys
    assert "renormalized" in (metrics.overall.note or "")
