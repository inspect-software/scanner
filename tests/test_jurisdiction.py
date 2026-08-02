from scanner.jurisdiction import assess_repo, classify_location
from scanner.metrics import compute_metrics, metric_high_risk_jurisdiction_exposure
from scanner.models import (
    Activity,
    CommunityHealth,
    Contributor,
    ContributorOrganization,
    ContributorProfile,
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


def test_explicit_policy_countries_and_native_names_are_high_confidence():
    assert classify_location("Moscow, Russia").country_code == "RU"
    assert classify_location("Tehran, ایران").country_code == "IR"
    assert classify_location("Pyongyang, DPRK").country_code == "KP"
    assert classify_location("🇰🇵").confidence == "high"


def test_place_and_region_gazetteer_matches_without_network():
    assert classify_location("Moscow").country_code == "RU"
    assert classify_location("Tehran").country_code == "IR"
    assert classify_location("Pyongyang").country_code == "KP"


def test_conflicting_country_or_us_state_is_review_only():
    assert classify_location("Moscow, Idaho").confidence == "review"
    assert classify_location("St Petersburg, FL").confidence == "review"
    assert classify_location("Russia / Germany").confidence == "review"


def test_internationally_recognized_ukrainian_places_are_not_russia_matches():
    assert classify_location("Crimea") is None
    assert classify_location("Donetsk, Ukraine") is None


def test_owner_exposure_is_critical_multiplier_and_aggregated_without_identity():
    data = RepoData(owner=OwnerProfile(login="private-login", type="Organization", location="Russia"))
    metric = metric_high_risk_jurisdiction_exposure(data)
    assert metric.value == 20
    assert metric.band == "critical"
    assert metric.inputs["red_flag"] is True
    assert "private-login" not in str(metric.inputs)


def test_safe_location_does_not_raise_security_score():
    safe = RepoData(owner=OwnerProfile(login="acme", type="Organization", location="Berlin, Germany"))
    unknown = RepoData(owner=OwnerProfile(login="acme", type="Organization"))
    safe_security = compute_metrics(safe).category("security").value
    unknown_security = compute_metrics(unknown).category("security").value
    assert safe_security == unknown_security


def test_owner_red_flag_multiplies_existing_security_posture():
    security = SecuritySignals(
        scorecard=Scorecard(checks=[ScorecardCheck(name="Vulnerabilities", score=10)])
    )
    safe = RepoData(
        owner=OwnerProfile(login="acme", type="Organization", location="Berlin, Germany"),
        security_signals=security,
    )
    flagged = RepoData(
        owner=OwnerProfile(login="acme", type="Organization", location="Tehran, Iran"),
        security_signals=security,
    )
    safe_metrics = compute_metrics(safe)
    flagged_metrics = compute_metrics(flagged)
    base = safe_metrics.by_key("security_posture").value
    assert safe_metrics.category("security").value == base
    assert base == 100
    assert flagged_metrics.by_key("security_posture").value == 20
    assert flagged_metrics.category("security").value == 20
    assert flagged_metrics.by_key("security_posture").inputs[
        "security_posture_before_jurisdiction"
    ] == 100
    assert flagged_metrics.overall.inputs["high_risk_jurisdiction_multiplier"] == 20


def test_red_flag_caps_overall_health_at_at_risk():
    data = RepoData(
        owner=OwnerProfile(
            login="acme", type="Organization", location="Berlin, Germany",
            is_verified=True, followers=1000, public_repos=30, account_age_days=2000,
        ),
        repo=RepoInfo(homepage="https://example.com", topics=["security"], has_wiki=True),
        popularity=Popularity(stars=5000, forks=500, watchers=200),
        activity=Activity(
            days_since_last_push=1, active_weeks_last_year=50, commits_last_year=500,
            releases_count=20, days_since_latest_release=10, mean_days_between_releases=20.0,
        ),
        maintainership=Maintainership(
            bus_factor=5, top_contributor_share=0.2, contributors_sampled=40,
            issues=IssueMetrics(closed_ratio=0.9, merged_prs=100, closed_unmerged_prs=5),
            top_contributors=[
                Contributor(
                    login="alice",
                    commits=100,
                    profile=ContributorProfile(
                        organizations=[
                            ContributorOrganization(login="example", location="Iran")
                        ]
                    ),
                )
            ],
        ),
        community=CommunityHealth(
            has_readme=True, has_license=True, has_contributing=True, has_description=True,
        ),
        quality_signals=QualitySignals(
            has_ci=True, has_tests=True, has_docs_dir=True, has_linter_config=True,
        ),
        security_signals=SecuritySignals(
            scorecard=Scorecard(checks=[ScorecardCheck(name="Vulnerabilities", score=10)])
        ),
    )

    metrics = compute_metrics(data)
    assert metrics.overall.value == 49
    assert metrics.overall.band == "at_risk"
    assert metrics.by_key("security_posture").value == 49
    assert metrics.category("security").value == 49
    assert metrics.overall.inputs["weighted_overall_before_jurisdiction"] > 65
    assert metrics.overall.inputs["high_risk_jurisdiction_multiplier"] == 75
    assert metrics.overall.inputs["overall_after_jurisdiction_multiplier"] > 49
    assert metrics.overall.inputs["high_risk_jurisdiction_cap"] == 49
    assert "multiplier" in metrics.overall.note
    assert "At risk ceiling" in metrics.overall.note


def test_top_contributor_and_public_org_membership_have_weaker_multipliers():
    contributor = Contributor(
        login="alice",
        commits=60,
        profile=ContributorProfile(
            location="North Korea",
            organizations=[ContributorOrganization(login="example", location="Iran")],
        ),
    )
    data = RepoData(maintainership=Maintainership(top_contributors=[contributor]))
    assessment = assess_repo(data)
    assert {item.role for item in assessment.exposures} == {
        "top_contributor", "contributor_organization"
    }
    assert metric_high_risk_jurisdiction_exposure(data).value == 50


def _repo_with_contributor(commits: int, top_contributor_share: float | None, *,
                           others: list[int] = ()) -> RepoData:
    """One policy-scope contributor plus optional clean higher-ranked ones."""
    top = [
        Contributor(login=f"clean{i}", commits=c, profile=ContributorProfile(location="Berlin"))
        for i, c in enumerate(others)
    ]
    top.append(
        Contributor(
            login="alice", commits=commits,
            profile=ContributorProfile(location="Moscow, Russia"),
        )
    )
    return RepoData(
        maintainership=Maintainership(
            top_contributors=top, top_contributor_share=top_contributor_share
        )
    )


def test_minor_contributor_match_is_recorded_but_not_scored():
    # 8 commits out of ~2000 (0.4%): a drive-by contributor, not stewardship.
    data = _repo_with_contributor(8, 0.5, others=[1000])
    metric = metric_high_risk_jurisdiction_exposure(data)
    assert metric.value == 100
    assert metric.inputs["red_flag"] is False
    assert metric.inputs["exposures"] == []
    assert metric.inputs["below_threshold_exposures"] == [
        {"country": "Russia", "role": "top_contributor", "count": 1}
    ]
    assert "below the commit-weight threshold" in metric.note


def test_contributor_with_material_commit_count_still_fires():
    # 84 commits is a substantial body of work even at a small share.
    data = _repo_with_contributor(84, 0.24, others=[1088])
    metric = metric_high_risk_jurisdiction_exposure(data)
    assert metric.value == 50
    assert metric.inputs["red_flag"] is True


def test_small_repo_co_maintainer_fires_via_share_leg():
    # 30 commits of 200 (15%): few absolute commits, but a real co-maintainer.
    data = _repo_with_contributor(30, 0.5, others=[100])
    metric = metric_high_risk_jurisdiction_exposure(data)
    assert metric.value == 50
    assert metric.inputs["red_flag"] is True


def test_below_threshold_contributor_does_not_carry_their_org_exposure():
    contributor = Contributor(
        login="alice",
        commits=3,
        profile=ContributorProfile(
            location="Moscow, Russia",
            organizations=[ContributorOrganization(login="example", location="Iran")],
        ),
    )
    data = RepoData(
        maintainership=Maintainership(
            top_contributors=[
                Contributor(login="clean", commits=997,
                            profile=ContributorProfile(location="Berlin")),
                contributor,
            ],
            top_contributor_share=0.997,
        )
    )
    metric = metric_high_risk_jurisdiction_exposure(data)
    assert metric.value == 100
    assert metric.inputs["red_flag"] is False
    assert {row["role"] for row in metric.inputs["below_threshold_exposures"]} == {
        "top_contributor", "contributor_organization"
    }


def test_owner_exposure_is_never_gated_by_commit_weight():
    data = RepoData(
        owner=OwnerProfile(login="acme", type="User", location="Russia"),
        maintainership=Maintainership(top_contributors=[]),
    )
    metric = metric_high_risk_jurisdiction_exposure(data)
    assert metric.value == 20
    assert metric.inputs["red_flag"] is True


def test_ambiguous_match_never_changes_score():
    data = RepoData(owner=OwnerProfile(login="acme", type="User", location="Moscow, Idaho"))
    metric = metric_high_risk_jurisdiction_exposure(data)
    assert metric.value == 100
    assert metric.inputs["review_only_matches"] == 1
    assert metric.inputs["red_flag"] is False
