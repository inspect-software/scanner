from scanner.metrics import (
    METRICS_VERSION,
    REPO_CATEGORIES,
    band_for,
    compute_metrics,
    metric_development_activity,
    metric_documentation,
    metric_ecosystem_adoption,
    metric_maintainer_resilience,
    metric_package_maintenance,
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
    EcosystemData,
    EcosystemPackage,
    IssueMetrics,
    Maintainership,
    OwnerProfile,
    Popularity,
    QualitySignals,
    RepoData,
    RepoInfo,
    Scorecard,
    ScorecardCheck,
    SecuritySignals,
)


def _pkg(**kw):
    base = dict(ecosystem="npm", name="thing", registry_url="x", matches_repo=True)
    base.update(kw)
    return EcosystemPackage(**base)


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


def _scorecard_data(*checks: ScorecardCheck) -> SecuritySignals:
    return SecuritySignals(scorecard=Scorecard(checks=list(checks)))


def test_scorecard_maintenance_evidence_enriches_vitality():
    activity = Activity(days_since_last_push=300, active_weeks_last_year=10, commits_last_year=10)
    without = metric_development_activity(RepoData(activity=activity))
    with_scorecard = metric_development_activity(RepoData(
        activity=activity,
        security_signals=_scorecard_data(ScorecardCheck(name="Maintained", score=10)),
    ))
    assert with_scorecard.value > without.value
    assert {c.name for c in with_scorecard.components} >= {"OpenSSF Scorecard: Maintained"}


def test_inconclusive_cross_category_scorecard_evidence_is_excluded():
    metric = metric_development_activity(RepoData(
        activity=Activity(days_since_last_push=2, active_weeks_last_year=50, commits_last_year=400),
        security_signals=_scorecard_data(ScorecardCheck(name="Maintained", score=None)),
    ))
    by = {c.name: c for c in metric.components}
    assert by["OpenSSF Scorecard: Maintained"].status == "excluded"


def test_scorecard_cross_category_checks_are_attached_to_their_metric_cards():
    data = RepoData(
        activity=Activity(days_since_last_push=2, active_weeks_last_year=50, commits_last_year=400,
                          releases_count=10, days_since_latest_release=20, mean_days_between_releases=30),
        maintainership=Maintainership(
            bus_factor=4, top_contributor_share=0.4, contributors_sampled=10,
            issues=IssueMetrics(closed_ratio=0.8, merged_prs=8, closed_unmerged_prs=2),
        ),
        community=CommunityHealth(has_readme=True, has_license=True),
        quality_signals=QualitySignals(has_ci=True, has_tests=True),
        security_signals=_scorecard_data(
            ScorecardCheck(name="Maintained", score=10),
            ScorecardCheck(name="Signed-Releases", score=10),
            ScorecardCheck(name="Contributors", score=10),
            ScorecardCheck(name="Code-Review", score=10),
            ScorecardCheck(name="License", score=10),
            ScorecardCheck(name="CI-Tests", score=10),
            ScorecardCheck(name="Pinned-Dependencies", score=10),
        ),
    )
    metrics = compute_metrics(data)
    expected = {
        "development_activity": "Maintained",
        "release_discipline": "Signed-Releases",
        "maintainer_resilience": "Contributors",
        "responsiveness": "Code-Review",
        "community_health": "License",
        "engineering_practices": "CI-Tests",
        "ai_verify_loop": "Pinned-Dependencies",
    }
    for metric_key, check_name in expected.items():
        metric = metrics.by_key(metric_key)
        assert metric is not None
        assert f"OpenSSF Scorecard: {check_name}" in {component.name for component in metric.components}


# --- release discipline (new) -------------------------------------------------


def test_release_discipline_none_when_unknown():
    assert metric_release_discipline(RepoData()) is None


def test_release_discipline_no_releases_missed():
    m = metric_release_discipline(RepoData(activity=Activity(releases_count=0)))
    assert m.value <= 3
    assert all(c.status in ("missed", "excluded") for c in m.components)


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


def test_popularity_ignores_trivial_counts():
    # Values of 1 or 2 stars/forks/watchers earn nothing; scoring starts at 3.
    for count in (1, 2):
        m = metric_popularity(RepoData(popularity=Popularity(
            stars=count, forks=count, watchers=count)))
        by_name = {c.name: c for c in m.components}
        assert by_name["Stars"].points == 0.0
        assert by_name["Forks"].points == 0.0
        assert by_name["Watchers"].points == 0.0
    threes = metric_popularity(RepoData(popularity=Popularity(
        stars=3, forks=3, watchers=3)))
    by_name = {c.name: c for c in threes.components}
    assert by_name["Stars"].points > 0.0
    assert by_name["Forks"].points > 0.0
    assert by_name["Watchers"].points > 0.0


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


# --- ecosystem metrics (new) --------------------------------------------------


def test_ecosystem_metrics_none_without_packages():
    assert metric_ecosystem_adoption(RepoData()) is None
    assert metric_package_maintenance(RepoData()) is None


def test_ecosystem_adoption_downloads():
    data = RepoData(ecosystem=EcosystemData(packages=[_pkg(monthly_downloads=2_000_000)]))
    m = metric_ecosystem_adoption(data)
    assert m.band == "excellent"
    by = {c.name: c for c in m.components}
    assert by["Monthly downloads"].status == "met"
    assert by["Registry dependents"].status == "excluded"  # npm reports none


def test_ecosystem_adoption_total_downloads_fallback():
    # RubyGems exposes only total downloads (no monthly) — must still score
    data = RepoData(ecosystem=EcosystemData(packages=[
        _pkg(ecosystem="rubygems", monthly_downloads=None, total_downloads=40_000_000)]))
    m = metric_ecosystem_adoption(data)
    assert m is not None
    by = {c.name: c for c in m.components}
    assert "Total downloads" in by
    assert by["Total downloads"].status in ("met", "partial")


def test_ecosystem_adoption_excluded_repo_not_counted():
    # a package whose registry repo points elsewhere is not this repo's package
    data = RepoData(ecosystem=EcosystemData(packages=[
        _pkg(monthly_downloads=5_000_000, matches_repo=False)]))
    assert metric_ecosystem_adoption(data) is None


def test_package_maintenance_healthy():
    data = RepoData(ecosystem=EcosystemData(packages=[
        _pkg(days_since_latest_publish=30, versions_count=20)]))
    m = metric_package_maintenance(data)
    assert m.band in ("good", "excellent")
    assert all(c.status == "met" for c in m.components)


def test_package_maintenance_deprecated_penalized():
    data = RepoData(ecosystem=EcosystemData(packages=[
        _pkg(days_since_latest_publish=900, versions_count=3,
             is_deprecated=True, deprecation_note="use foo")]))
    m = metric_package_maintenance(data)
    by = {c.name: c for c in m.components}
    assert by["Not deprecated"].status == "missed"
    assert by["Publish recency"].status in ("missed", "partial")
    assert m.value < 50


def test_ecosystem_adoption_in_community_category():
    data = RepoData(
        popularity=Popularity(stars=100),
        community=CommunityHealth(has_readme=True),
        ecosystem=EcosystemData(packages=[_pkg(monthly_downloads=1_000_000)]),
    )
    metrics = compute_metrics(data)
    community = metrics.category("community")
    keys = {m.key for m in community.metrics}
    assert "ecosystem_adoption" in keys


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


def test_security_lockfile_excluded_for_published_library():
    # A published library/gem (e.g. a Ruby gem like puma) declares dependencies
    # but is expected NOT to commit a lockfile — that is an application concern.
    # Its absence must be excluded and renormalized, not scored as a miss.
    signals = SecuritySignals(has_security_policy=True, has_dependabot_config=True)
    library = RepoData(
        security_signals=signals,
        dependencies=DependencySignals(manifests=["Gemfile"]),
        ecosystem=EcosystemData(packages=[_pkg(ecosystem="rubygems", name="puma")]),
    )
    application = RepoData(
        security_signals=signals,
        dependencies=DependencySignals(manifests=["Gemfile"]),
    )
    lib_metric = metric_security_posture(library)
    app_metric = metric_security_posture(application)
    lib_by = {c.name: c for c in lib_metric.components}
    app_by = {c.name: c for c in app_metric.components}
    # library: lockfile excluded (renormalized); application: lockfile missed
    assert lib_by["Dependency lockfiles"].status == "excluded"
    assert app_by["Dependency lockfiles"].status == "missed"
    # so the lockfile-less library is not dragged down for it
    assert lib_metric.value > app_metric.value


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
    assert keys == {"vitality", "community", "governance", "engineering", "security", "ai_readiness"}
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
