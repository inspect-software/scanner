from datetime import datetime, timezone

import pytest

from scanner.github import parse_target
from scanner.metrics import (
    compute_org_metrics,
    metric_org_portfolio,
    metric_org_profile,
    metric_org_reach,
)
from scanner.models import OrgData, OrgInfo, OrgPortfolio, OrgRef, OrgReport
from scanner.render import render_org_html


# --- target classification ---------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("pallets/flask", ("repo", "pallets", "flask")),
        ("https://github.com/pallets/flask", ("repo", "pallets", "flask")),
        ("git@github.com:pallets/flask.git", ("repo", "pallets", "flask")),
        ("python", ("org", "python")),
        ("https://github.com/python", ("org", "python")),
        ("https://github.com/python/", ("org", "python")),
    ],
)
def test_parse_target(target, expected):
    assert parse_target(target) == expected


def test_parse_target_invalid():
    with pytest.raises(ValueError):
        parse_target("https://gitlab.com/whatever")
    with pytest.raises(ValueError):
        parse_target("not a target at all")


# --- org metrics --------------------------------------------------------------


def full_profile() -> OrgInfo:
    return OrgInfo(
        login="acme",
        name="Acme Corp",
        description="We make everything",
        blog="https://acme.example",
        location="Earth",
        email="oss@acme.example",
        twitter_username="acme",
        is_verified=True,
        public_repos=120,
        followers=5000,
    )


def test_org_profile_full_scores_100():
    m = metric_org_profile(OrgData(info=full_profile()))
    assert m.value == 100
    assert all(c.status == "met" for c in m.components)


def test_org_profile_empty_scores_low():
    m = metric_org_profile(OrgData(info=OrgInfo(login="ghost")))
    assert m.value == 1
    assert m.band == "critical"


def test_org_portfolio_active():
    data = OrgData(
        info=full_profile(),
        portfolio=OrgPortfolio(
            repos_sampled=100,
            repos_pushed_90d=80,
            repos_pushed_365d=95,
            original_repos_sampled=90,
            forks_sampled=10,
            total_stars_sampled=20000,
        ),
    )
    m = metric_org_portfolio(data)
    assert m.band in ("good", "excellent")
    by_name = {c.name: c for c in m.components}
    assert by_name["Recently active repos"].status == "partial"
    assert by_name["Portfolio size"].status == "met"  # 120 repos saturates log scale


def test_org_portfolio_no_repos_excludes_activity():
    m = metric_org_portfolio(OrgData(info=OrgInfo(login="x", public_repos=0)))
    by_name = {c.name: c for c in m.components}
    assert by_name["Recently active repos"].status == "excluded"
    assert by_name["Original work"].status == "excluded"


def test_org_reach_log_scale():
    small = metric_org_reach(OrgData(info=OrgInfo(login="a", followers=10)))
    big = metric_org_reach(
        OrgData(
            info=OrgInfo(login="b", followers=5000),
            portfolio=OrgPortfolio(total_stars_sampled=50000, repos_sampled=100),
        )
    )
    assert small.value < big.value
    assert big.value >= 85


def test_compute_org_metrics_overall():
    data = OrgData(
        info=full_profile(),
        portfolio=OrgPortfolio(
            repos_sampled=100, repos_pushed_90d=70, repos_pushed_365d=90,
            original_repos_sampled=95, forks_sampled=5, total_stars_sampled=30000,
        ),
    )
    metrics = compute_org_metrics(data)
    assert metrics.overall is not None
    assert metrics.overall.value >= 70
    assert set(metrics.overall.inputs) == {
        "profile_completeness", "portfolio_activity", "community_reach",
    }


# --- org rendering ------------------------------------------------------------


def test_render_org_html():
    data = OrgData(info=full_profile(), portfolio=OrgPortfolio(repos_sampled=10))
    report = OrgReport(
        generated_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        source=OrgRef(url="https://github.com/acme", login="acme"),
        data=data,
        metrics=compute_org_metrics(data),
        warnings=[],
    )
    html = render_org_html(report)
    assert html.startswith("<!DOCTYPE html>")
    assert "Acme Corp" in html
    assert "Organization Health Report" in html
    for name in ("Portfolio activity", "Community reach", "Profile completeness"):
        assert name in html
    assert "verified domain" in html
