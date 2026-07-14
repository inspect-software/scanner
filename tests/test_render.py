from datetime import datetime, timezone

from scanner.metrics import compute_metrics
from scanner.models import (
    Activity,
    CommunityHealth,
    IssueMetrics,
    Maintainership,
    OwnerProfile,
    Popularity,
    QualitySignals,
    RepoData,
    RepoInfo,
    RepoRef,
    Report,
)
from scanner.render import render_html


_DEFAULT_OWNER = OwnerProfile(login="acme", type="Organization", name="Acme Inc",
                              is_verified=True, followers=900, public_repos=30,
                              account_age_days=2000)


def make_report(owner=_DEFAULT_OWNER, **overrides) -> Report:
    data = RepoData(
        owner=owner,
        repo=RepoInfo(owner_type=(owner.type if owner else None),
                      description="A <test> repo", primary_language="Python",
                      license_spdx="MIT", homepage="https://x.io", topics=["a"], has_wiki=True),
        popularity=Popularity(stars=1234, forks=56, watchers=20),
        activity=Activity(
            days_since_last_push=2,
            active_weeks_last_year=40,
            commits_last_year=300,
            releases_count=20,
            days_since_latest_release=30,
            mean_days_between_releases=20.0,
        ),
        maintainership=Maintainership(
            bus_factor=3,
            top_contributor_share=0.4,
            contributors_sampled=25,
            issues=IssueMetrics(closed_ratio=0.8, merged_prs=50, closed_unmerged_prs=5),
        ),
        community=CommunityHealth(has_readme=True, has_license=True),
        quality_signals=QualitySignals(has_ci=True, has_tests=True),
    )
    return Report(
        generated_at=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
        source=RepoRef(url="acme/widget", owner="acme", name="widget"),
        data=data,
        metrics=compute_metrics(data),
        warnings=["Sample warning"],
        **overrides,
    )


def test_render_produces_full_page():
    html = render_html(make_report())
    assert html.startswith("<!DOCTYPE html>")
    assert "acme" in html and "widget" in html
    assert "overall / 100" in html
    # all ten metrics present by name
    for name in (
        "Development activity", "Release discipline", "Popularity &amp; adoption",
        "Maintainer resilience", "Issue &amp; PR responsiveness", "Ownership &amp; stewardship",
        "Community health", "Engineering practices", "Documentation", "Security posture",
    ):
        assert name in html, name
    # all five categories present
    for cat in ("Vitality", "Community &amp; Adoption", "Sustainability &amp; Governance",
                "Engineering Quality", "Security"):
        assert cat in html, cat
    assert "How it&#39;s scored" in html or "How it's scored" in html
    assert "Excellent" in html and "Critical" in html
    assert "Sample warning" in html


def test_render_escapes_untrusted_text():
    html = render_html(make_report())
    assert "A &lt;test&gt; repo" in html
    assert "A <test> repo" not in html


def test_render_marks_component_statuses():
    html = render_html(make_report())
    assert 'class="comp-met"' in html
    assert 'class="comp-missed"' in html
    assert 'data-lucide="circle-check"' in html
    assert 'data-lucide="circle-x"' in html


def test_render_ownership_section_org():
    owner = OwnerProfile(login="acme", type="Organization", name="Acme Inc",
                         is_verified=True, followers=900, public_repos=30, account_age_days=2000)
    html = render_html(make_report(owner=owner))
    assert ">Ownership<" in html
    assert "Acme Inc" in html
    assert "Organization" in html
    assert "backed by an <b>organization</b>" in html
    assert 'id="metric-stewardship"' in html


def test_render_ownership_section_user():
    owner = OwnerProfile(login="joe", type="User", name="Joe Dev",
                         followers=40, public_repos=10, account_age_days=1500)
    html = render_html(make_report(owner=owner))
    assert "Personal account" in html
    assert "owned by a <b>personal account</b>" in html


def test_render_no_ownership_when_absent():
    html = render_html(make_report(owner=None))
    # no owner data -> no Ownership section heading
    assert ">Ownership<" not in html


def test_render_ecosystem_section():
    from scanner.models import EcosystemData, EcosystemPackage
    report = make_report()
    report.data.ecosystem = EcosystemData(packages=[
        EcosystemPackage(ecosystem="pypi", name="widgetlib", registry_url="https://pypi.org/project/widgetlib/",
                         latest_version="2.1.0", monthly_downloads=1234567, versions_count=15,
                         days_since_latest_publish=12, matches_repo=True, keywords=["widgets", "gui"]),
    ])
    report.metrics = compute_metrics(report.data)
    html = render_html(report)
    assert "Package ecosystems" in html
    assert "widgetlib" in html
    assert "PyPI" in html
    assert "1,234,567" in html
    assert "widgets" in html and "gui" in html
    # adoption + maintenance metrics now present
    assert "Ecosystem adoption" in html
    assert "Package maintenance" in html


def test_render_no_ecosystem_section_when_no_packages():
    html = render_html(make_report())
    assert "Package ecosystems" not in html


def test_render_dependencies_section():
    from scanner.models import Dependency

    report = make_report()
    report.data.dependencies.dependencies = [
        Dependency(ecosystem="pypi", name="requests", version_constraint=">=2.0", manifest="pyproject.toml"),
        Dependency(ecosystem="npm", name="lodash", version_constraint=None, manifest="package.json"),
    ]
    html = render_html(report)
    assert "Declared dependencies" in html
    assert "requests" in html and "&gt;=2.0" in html
    assert "lodash" in html
    assert "pyproject.toml" in html


def test_render_no_dependencies_section_when_none_declared():
    html = render_html(make_report())
    assert "Declared dependencies" not in html
