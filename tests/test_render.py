from datetime import datetime, timezone

from scanner.metrics import compute_metrics
from scanner.models import (
    Activity,
    CommunityHealth,
    IssueMetrics,
    Maintainership,
    Popularity,
    QualitySignals,
    RepoData,
    RepoInfo,
    RepoRef,
    Report,
)
from scanner.render import render_html


def make_report(**overrides) -> Report:
    data = RepoData(
        repo=RepoInfo(description="A <test> repo", primary_language="Python", license_spdx="MIT"),
        popularity=Popularity(stars=1234, forks=56),
        activity=Activity(
            days_since_last_push=2,
            active_weeks_last_year=40,
            commits_last_year=300,
            releases_count=20,
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
    report = Report(
        generated_at=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
        source=RepoRef(url="acme/widget", owner="acme", name="widget"),
        data=data,
        metrics=compute_metrics(data),
        warnings=["Sample warning"],
        **overrides,
    )
    return report


def test_render_produces_full_page():
    html = render_html(make_report())
    assert html.startswith("<!DOCTYPE html>")
    assert "acme" in html and "widget" in html
    # score hero and all metric cards present
    assert "overall / 100" in html
    for name in (
        "Development activity",
        "Maintainer resilience",
        "Issue &amp; PR responsiveness",
        "Community health",
        "Engineering practices",
        "Security posture",
    ):
        assert name in html
    # explanations and band scale present
    assert "How it&#39;s scored" in html or "How it's scored" in html
    assert "Excellent" in html and "Critical" in html
    # warnings surfaced
    assert "Sample warning" in html


def test_render_escapes_untrusted_text():
    html = render_html(make_report())
    assert "A &lt;test&gt; repo" in html
    assert "A <test> repo" not in html


def test_render_marks_component_statuses():
    html = render_html(make_report())
    # met, missed and excluded statuses all occur in the fixture report
    assert 'class="comp-met"' in html
    assert 'class="comp-missed"' in html
    assert 'data-lucide="circle-check"' in html
    assert 'data-lucide="circle-x"' in html
    # earned/max points rendered (CI met: 30/30, docs dir missed: 0/10)
    assert "30/30" in html
    assert "0/10" in html


def test_render_without_metrics():
    report = make_report()
    report = report.model_copy(update={"metrics": None})
    html = render_html(report)
    assert "Not enough public data" in html
