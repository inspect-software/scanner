from scanner.jurisdiction import assess_repo, classify_location
from scanner.metrics import compute_metrics, metric_high_risk_jurisdiction_exposure
from scanner.models import (
    Contributor,
    ContributorOrganization,
    ContributorProfile,
    Maintainership,
    OwnerProfile,
    RepoData,
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
    assert flagged_metrics.category("security").value == 20


def test_top_contributor_and_public_org_membership_have_weaker_multipliers():
    contributor = Contributor(
        login="alice",
        commits=10,
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


def test_ambiguous_match_never_changes_score():
    data = RepoData(owner=OwnerProfile(login="acme", type="User", location="Moscow, Idaho"))
    metric = metric_high_risk_jurisdiction_exposure(data)
    assert metric.value == 100
    assert metric.inputs["review_only_matches"] == 1
    assert metric.inputs["red_flag"] is False
