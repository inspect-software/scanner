"""Scan-configuration behavior: enabling/disabling metrics, categories and
components, and how that flows into scores, reports and HTML."""

from datetime import datetime, timezone

import pytest

from scanner.cli import build_config, build_parser
from scanner.metrics import (
    compute_metrics,
    known_category_keys,
    known_metric_keys,
    validate_config,
)
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
    ScanConfig,
)
from scanner.render import render_html


def _rich_data() -> RepoData:
    return RepoData(
        owner=OwnerProfile(login="acme", type="Organization", is_verified=True,
                           followers=900, public_repos=30, account_age_days=2000),
        repo=RepoInfo(homepage="https://x.io", topics=["a"], has_wiki=True,
                      primary_language="Python"),
        popularity=Popularity(stars=3000, forks=400, watchers=100),
        activity=Activity(days_since_last_push=2, active_weeks_last_year=45,
                          commits_last_year=300, releases_count=20,
                          days_since_latest_release=15, mean_days_between_releases=20.0),
        maintainership=Maintainership(bus_factor=4, top_contributor_share=0.3,
                                      contributors_sampled=30,
                                      issues=IssueMetrics(closed_ratio=0.85, merged_prs=90,
                                                          closed_unmerged_prs=5)),
        community=CommunityHealth(has_readme=True, has_license=True, has_contributing=True,
                                  has_description=True),
        quality_signals=QualitySignals(has_ci=True, has_tests=True, has_docs_dir=True,
                                        has_linter_config=True),
    )


def _parse(argv):
    return build_parser().parse_args(argv)


# --- model basics -------------------------------------------------------------


def test_default_config_is_default():
    assert ScanConfig().is_default is True
    assert ScanConfig(disabled_metrics=["popularity"]).is_default is False


def test_default_config_matches_no_config():
    data = _rich_data()
    assert compute_metrics(data).overall.value == compute_metrics(data, ScanConfig()).overall.value


# --- disabling a category -----------------------------------------------------


def test_disable_category_drops_and_renormalizes():
    data = _rich_data()
    full = compute_metrics(data)
    custom = compute_metrics(data, ScanConfig(disabled_categories=["security"]))
    assert "security" in {c.key for c in full.categories}
    assert "security" not in {c.key for c in custom.categories}
    # security was the lowest category here, so removing it lifts the overall
    assert custom.overall.value > full.overall.value
    assert "security" not in custom.overall.inputs
    assert "disabled in scan configuration" in (custom.overall.note or "").lower()


# --- disabling a metric -------------------------------------------------------


def test_disable_metric_removed_from_category():
    data = _rich_data()
    custom = compute_metrics(data, ScanConfig(disabled_metrics=["popularity"]))
    community = custom.category("community")
    assert community is not None
    assert "popularity" not in {m.key for m in community.metrics}
    assert custom.by_key("popularity") is None


def test_disable_every_metric_in_category_drops_category():
    data = _rich_data()
    # Security has a single metric; disabling it removes the whole category.
    custom = compute_metrics(data, ScanConfig(disabled_metrics=["security_posture"]))
    assert "security" not in {c.key for c in custom.categories}


# --- disabling a component ----------------------------------------------------


def test_disable_component_excludes_and_notes():
    data = _rich_data()
    custom = compute_metrics(data, ScanConfig(disabled_components={"documentation": ["Wiki"]}))
    doc = custom.by_key("documentation")
    by = {c.name: c for c in doc.components}
    assert by["Wiki"].status == "excluded"
    assert by["Wiki"].detail == "disabled in scan configuration"
    assert by["Wiki"].points == 0.0
    assert "Wiki" in (doc.note or "")
    assert "renormalized" in (doc.note or "").lower()


def test_disable_component_changes_score_via_renormalization():
    # Documentation with only README + Wiki met; disabling Wiki renormalizes,
    # so the same met README is now a larger share of the (smaller) total.
    data = RepoData(
        repo=RepoInfo(has_wiki=True),
        community=CommunityHealth(has_readme=True),
    )
    full = compute_metrics(data).by_key("documentation")
    without_wiki = compute_metrics(
        data, ScanConfig(disabled_components={"documentation": ["Wiki"]})
    ).by_key("documentation")
    assert without_wiki.value != full.value


def test_disable_all_components_drops_metric():
    data = RepoData(popularity=Popularity(stars=100, forks=10, watchers=5))
    cfg = ScanConfig(disabled_components={"popularity": ["Stars", "Forks", "Watchers"]})
    metrics = compute_metrics(data, cfg)
    assert metrics.by_key("popularity") is None


# --- validation ---------------------------------------------------------------


def test_known_keys_cover_repo_and_org():
    assert {"vitality", "security"} <= known_category_keys()
    assert {"popularity", "security_posture", "portfolio_activity"} <= known_metric_keys()


def test_validate_config_flags_unknown_keys():
    warnings = validate_config(ScanConfig(
        disabled_categories=["nope"], disabled_metrics=["also_nope"],
        disabled_components={"missing_metric": ["x"]},
    ))
    assert any("nope" in w for w in warnings)
    assert any("also_nope" in w for w in warnings)
    assert any("missing_metric" in w for w in warnings)


def test_validate_config_accepts_known_keys():
    assert validate_config(ScanConfig(
        disabled_categories=["security"], disabled_metrics=["popularity"],
        disabled_components={"documentation": ["Wiki"]},
    )) == []


# --- report embedding ---------------------------------------------------------


def _report(config: ScanConfig) -> Report:
    data = _rich_data()
    return Report(
        generated_at=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
        source=RepoRef(url="acme/widget", owner="acme", name="widget"),
        config=config,
        data=data,
        metrics=compute_metrics(data, config),
    )


def test_report_embeds_and_roundtrips_config():
    cfg = ScanConfig(disabled_metrics=["popularity"], disabled_components={"documentation": ["Wiki"]})
    report = _report(cfg)
    restored = Report.model_validate_json(report.model_dump_json())
    assert restored.config == cfg


# --- rendering ----------------------------------------------------------------


def test_render_shows_full_methodology_by_default():
    html = render_html(_report(ScanConfig()))
    assert "Scan configuration" in html
    assert "Full methodology" in html


def test_render_lists_disabled_items():
    cfg = ScanConfig(
        disabled_categories=["security"],
        disabled_metrics=["popularity"],
        disabled_components={"documentation": ["Wiki"]},
    )
    html = render_html(_report(cfg))
    assert "customized configuration" in html
    assert "Disabled categories" in html and "Security" in html
    assert "Disabled metrics" in html and "Popularity" in html
    assert "Disabled components" in html and "Wiki" in html


# --- CLI config building ------------------------------------------------------


def test_build_config_from_flags():
    args = _parse([
        "owner/name",
        "--disable-category", "security",
        "--disable-metric", "popularity",
        "--disable-component", "documentation:Wiki",
    ])
    cfg = build_config(args)
    assert cfg.disabled_categories == ["security"]
    assert cfg.disabled_metrics == ["popularity"]
    assert cfg.disabled_components == {"documentation": ["Wiki"]}


def test_build_config_merges_file_and_flags(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text('{"disabled_metrics": ["popularity"]}', encoding="utf-8")
    args = _parse(["owner/name", "--config", str(path), "--disable-metric", "stewardship"])
    cfg = build_config(args)
    assert cfg.disabled_metrics == ["popularity", "stewardship"]


def test_build_config_rejects_malformed_component():
    args = _parse(["owner/name", "--disable-component", "no-colon-here"])
    with pytest.raises(ValueError):
        build_config(args)
