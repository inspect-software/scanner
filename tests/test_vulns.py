"""Advisory matching: pure parsing, summarizing, and metric behavior.

No network. ``collect_advisories`` is exercised through an httpx mock
transport so the failure paths are covered without hitting OSV.
"""

from __future__ import annotations

import httpx
import pytest

from scanner.metrics import metric_dependency_advisories
from scanner.models import (
    AdvisoryFinding,
    AllDependencies,
    DependencyAdvisories,
    RepoData,
    ResolvedDependency,
)
from scanner.vulns import (
    _score_vector,
    build_queries,
    cvss_base_score,
    penalty_units,
    severity_from_score,
    is_concrete_version,
    collect_advisories,
    fixed_version_of,
    osv_ecosystem,
    severity_of,
    summarize,
)


def dep(name, version="1.0.0", ecosystem="pypi", direct=True):
    return ResolvedDependency(ecosystem=ecosystem, name=name, version=version, direct=direct)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ours,expected",
    [("pypi", "PyPI"), ("npm", "npm"), ("crates", "crates.io"), ("go", "Go"), ("hex", "Hex")],
)
def test_ecosystem_labels_map_to_osv_spelling(ours, expected):
    assert osv_ecosystem(ours) == expected


def test_unknown_ecosystem_has_no_osv_equivalent():
    assert osv_ecosystem("cocoapods") is None


def test_packages_without_a_version_are_skipped_not_guessed():
    packages = [dep("flask"), dep("celery", version=None), dep("click")]
    queryable, queries, skipped = build_queries(packages)
    assert [p.name for p in queryable] == ["flask", "click"]
    assert len(queries) == 2
    assert skipped == 1


@pytest.mark.parametrize("value", ["4.0.0", "1.2.3-rc1", "v2.7.0", "2026.4.22"])
def test_concrete_versions_are_queryable(value):
    assert is_concrete_version(value) is True


@pytest.mark.parametrize(
    "value", ["^4.0.0", "~1.2", ">=2.0", "1.x", "1.2 - 1.9", "1.x || 2.x", "*", "", None]
)
def test_version_constraints_are_not_treated_as_versions(value):
    """SBOM entries sometimes carry the manifest range instead of the locked
    version; querying "^4.0.0" asks OSV about a string it will not read as the
    range means, and produced duplicate findings for the same package."""
    assert is_concrete_version(value) is False


def test_constraint_versions_are_skipped_and_counted():
    packages = [dep("form-data", "2.3.3"), dep("form-data", "^4.0.0")]
    queryable, queries, skipped = build_queries(packages)
    assert [p.version for p in queryable] == ["2.3.3"]
    assert len(queries) == 1
    assert skipped == 1


def test_packages_beyond_a_truncated_list_count_as_unassessed():
    def handler(request):
        return httpx.Response(200, json={"results": [{}]})

    with _client(handler) as client:
        result = collect_advisories(
            [dep("only-one")], [], total_packages=50, client=client
        )
    assert result.collected is True
    assert result.assessed_count == 1
    # 49 packages the graph counted but the report did not embed
    assert result.unassessed_count == 49


def test_unsupported_ecosystems_are_skipped():
    _, queries, skipped = build_queries([dep("Alamofire", ecosystem="cocoapods")])
    assert queries == []
    assert skipped == 1


def test_severity_reads_the_database_label():
    assert severity_of({"database_specific": {"severity": "HIGH"}}) == "high"


def test_severity_is_unknown_rather_than_guessed_when_absent():
    assert severity_of({}) == "unknown"
    assert severity_of({"database_specific": {"severity": "WEIRD"}}) == "unknown"


def test_fixed_version_takes_the_highest_stated_fix():
    detail = {
        "affected": [
            {"ranges": [{"events": [{"introduced": "0"}, {"fixed": "3.1.2"}]}]},
            {"ranges": [{"events": [{"fixed": "3.1.10"}]}]},
        ]
    }
    assert fixed_version_of(detail) == "3.1.10"


def test_commit_hashes_are_not_offered_as_upgrade_targets():
    detail = {
        "affected": [
            {"ranges": [{"events": [{"fixed": "f3c803b3ade485a45f12b6d6617595350c0f03e2"}]}]}
        ]
    }
    assert fixed_version_of(detail) is None


# --------------------------------------------------------------------------
# Summarizing
# --------------------------------------------------------------------------


def test_summarize_reports_worst_severity_and_coverage():
    packages = [dep("jinja2", "3.1.2"), dep("click", "8.1.3"), dep("safe", "1.0.0")]
    results = [
        {"vulns": [{"id": "GHSA-a"}, {"id": "GHSA-b"}]},
        {"vulns": [{"id": "PYSEC-1"}]},
        {},
    ]
    details = {
        "GHSA-a": {"database_specific": {"severity": "MODERATE"}},
        "GHSA-b": {
            "database_specific": {"severity": "HIGH"},
            "affected": [{"ranges": [{"events": [{"fixed": "3.1.6"}]}]}],
        },
    }
    summary = summarize(packages, results, details, skipped=2)

    assert summary.collected is True
    assert summary.assessed_count == 3
    assert summary.unassessed_count == 2
    assert summary.affected_count == 2
    assert summary.advisory_count == 3
    jinja = next(f for f in summary.findings if f.name == "jinja2")
    assert jinja.severity == "high"
    assert jinja.fixed_version == "3.1.6"
    assert jinja.advisory_count == 2
    # PYSEC record has no detail fetched -> reported unknown, never assumed clean
    assert next(f for f in summary.findings if f.name == "click").severity == "unknown"


def test_findings_are_ordered_most_severe_first():
    packages = [dep("low-one"), dep("crit-one")]
    results = [{"vulns": [{"id": "L"}]}, {"vulns": [{"id": "C"}]}]
    details = {
        "L": {"database_specific": {"severity": "LOW"}},
        "C": {"database_specific": {"severity": "CRITICAL"}},
    }
    summary = summarize(packages, results, details, skipped=0)
    assert [f.name for f in summary.findings] == ["crit-one", "low-one"]


# --------------------------------------------------------------------------
# Network path, mocked
# --------------------------------------------------------------------------


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_collect_advisories_happy_path():
    def handler(request):
        if request.url.path == "/v1/querybatch":
            return httpx.Response(200, json={"results": [{"vulns": [{"id": "GHSA-x"}]}, {}]})
        return httpx.Response(
            200,
            json={
                "id": "GHSA-x",
                "database_specific": {"severity": "CRITICAL"},
                "affected": [{"ranges": [{"events": [{"fixed": "2.0.0"}]}]}],
            },
        )

    warnings: list[str] = []
    with _client(handler) as client:
        result = collect_advisories([dep("bad"), dep("good")], warnings, client=client)

    assert result.collected is True
    assert result.affected_count == 1
    assert result.findings[0].severity == "critical"
    assert result.findings[0].fixed_version == "2.0.0"
    assert warnings == []


def test_transport_failure_is_recorded_and_never_raises():
    def handler(request):
        raise httpx.ConnectError("osv unreachable")

    warnings: list[str] = []
    with _client(handler) as client:
        result = collect_advisories([dep("flask")], warnings, client=client)

    assert result.collected is False
    assert result.error and "osv unreachable" in result.error
    assert warnings and warnings[0] == result.error


def test_result_count_mismatch_is_refused_rather_than_misaligned():
    def handler(request):
        return httpx.Response(200, json={"results": [{}]})

    warnings: list[str] = []
    with _client(handler) as client:
        result = collect_advisories([dep("a"), dep("b")], warnings, client=client)

    assert result.collected is False
    assert "2 queries" in (result.error or "")


def test_advisory_details_are_reused_from_the_cache():
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/querybatch":
            return httpx.Response(200, json={"results": [{"vulns": [{"id": "GHSA-x"}]}]})
        return httpx.Response(200, json={"id": "GHSA-x"})

    cache = {"GHSA-x": {"database_specific": {"severity": "LOW"}}}
    with _client(handler) as client:
        result = collect_advisories([dep("flask")], [], detail_cache=cache, client=client)

    assert result.findings[0].severity == "low"
    assert calls == ["/v1/querybatch"]  # no detail fetch


# --------------------------------------------------------------------------
# Metric
# --------------------------------------------------------------------------


def _repo_with(advisories: DependencyAdvisories) -> RepoData:
    data = RepoData()
    data.dependencies.all_dependencies = AllDependencies(collected=True)
    data.dependencies.advisories = advisories
    return data


def test_metric_is_excluded_when_the_graph_was_unavailable():
    assert metric_dependency_advisories(_repo_with(DependencyAdvisories())) is None


def test_clean_dependency_set_scores_full_marks():
    metric = metric_dependency_advisories(
        _repo_with(DependencyAdvisories(collected=True, source="osv", scope="repository_graph", assessed_count=120))
    )
    assert metric is not None
    assert metric.value == 100
    assert metric.band == "exceptional"


def test_direct_advisories_cost_more_than_indirect():
    def score(direct: bool) -> int:
        adv = DependencyAdvisories(
            collected=True,
            source="osv",
            scope="repository_graph",
            assessed_count=100,
            affected_count=1,
            direct_affected_count=1 if direct else 0,
            advisory_count=1,
            findings=[
                AdvisoryFinding(
                    ecosystem="pypi", name="x", version="1.0", direct=direct,
                    severity="high", advisory_count=1,
                )
            ],
        )
        metric = metric_dependency_advisories(_repo_with(adv))
        assert metric is not None
        return metric.value

    assert score(direct=True) < score(direct=False)


def test_severity_drives_the_penalty():
    def score(severity: str) -> int:
        adv = DependencyAdvisories(
            collected=True, source="osv", scope="repository_graph", assessed_count=100, affected_count=1, advisory_count=1,
            findings=[
                AdvisoryFinding(
                    ecosystem="pypi", name="x", version="1.0", direct=True,
                    severity=severity, advisory_count=1,
                )
            ],
        )
        metric = metric_dependency_advisories(_repo_with(adv))
        assert metric is not None
        return metric.value

    assert score("critical") < score("high") < score("moderate") < score("low")


def test_a_clean_component_does_not_claim_more_than_its_own_scope():
    """A clean direct component must not read as "the whole set is clean"."""
    adv = DependencyAdvisories(
        collected=True, source="osv", scope="repository_graph", assessed_count=75, affected_count=1,
        findings=[
            AdvisoryFinding(
                ecosystem="pypi", name="urllib3", version="2.6.3", direct=False,
                severity="high", advisory_count=4,
            )
        ],
    )
    metric = metric_dependency_advisories(_repo_with(adv))
    assert metric is not None
    clean = next(c for c in metric.components if c.name.startswith("Direct"))
    assert clean.detail == "no direct dependency carries a known advisory"
    assert "75" not in (clean.detail or "")


def test_severity_counts_render_as_text_not_a_dict_repr():
    adv = DependencyAdvisories(
        collected=True, source="osv", scope="repository_graph", assessed_count=10, affected_count=2,
        by_severity={"moderate": 3, "high": 2},
    )
    metric = metric_dependency_advisories(_repo_with(adv))
    assert metric is not None
    assert metric.inputs["affected_by_severity"] == "high 2, moderate 3"


def test_note_states_coverage_and_the_reachability_limit():
    adv = DependencyAdvisories(
        collected=True, source="osv", scope="repository_graph", assessed_count=102, unassessed_count=4
    )
    metric = metric_dependency_advisories(_repo_with(adv))
    assert metric is not None
    assert "102" in metric.note and "4 could not be assessed" in metric.note
    assert "Reachability is not analyzed" in metric.note
    assert "development and test pins" in metric.note


def test_published_package_scope_names_what_was_installed():
    """In published-package scope the note must describe the shipped artifact,
    not the repository's build-and-test graph."""
    adv = DependencyAdvisories(
        collected=True, source="osv", scope="published_package",
        assessed_package="pypi:flask@3.1.3", assessed_count=7,
    )
    metric = metric_dependency_advisories(_repo_with(adv))
    assert metric is not None
    assert "pypi:flask@3.1.3" in metric.note
    assert "installing the published package pulls in" in metric.note
    assert "development and test pins" not in metric.note


# --------------------------------------------------------------------------
# CVSS-based severity (1.6.0)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector,expected",
    [
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L", 5.3),
        ("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N", 1.8),
        # Scope-changed vectors: the sum is multiplied by 1.08 (CVSS v3.1 8.1).
        # A wrong multiplier here pushed every S:C advisory into the 10.0 cap,
        # so unrelated packages all scored identically maximal.
        ("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:C/C:H/I:H/A:N", 8.7),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", 7.5),
        ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 7.8),
    ],
)
def test_cvss_vectors_score_to_the_published_base_score(vector, expected):
    assert _score_vector(vector) == pytest.approx(expected, abs=0.05)


def test_a_scope_change_does_not_saturate_the_scale():
    """S:C raises a score, it does not max it. Distinct advisories must remain
    distinguishable rather than all landing on 10.0."""
    a = _score_vector("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:C/C:H/I:H/A:N")
    b = _score_vector("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N")
    assert a < 10.0 and b < 10.0 and a != b


def test_cvss_v4_impact_metrics_are_understood():
    """v4.0 renames C/I/A to VC/VI/VA; an unhandled rename would silently drop
    every v4 advisory to the coarse-label fallback."""
    score = _score_vector("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N")
    assert score is not None and score >= 9.0


def test_unparseable_vectors_fall_back_rather_than_raise():
    assert _score_vector("not-a-vector") is None
    assert _score_vector("CVSS:3.1/AV:X/AC:L") is None


def test_highest_score_across_advisories_wins():
    detail = {"severity": [
        {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L"},
        {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},
    ]}
    assert cvss_base_score(detail) == pytest.approx(9.8, abs=0.05)


@pytest.mark.parametrize(
    "score,label", [(9.8, "critical"), (7.5, "high"), (5.3, "moderate"), (2.1, "low")]
)
def test_scores_map_to_the_standard_qualitative_bands(score, label):
    assert severity_from_score(score) == label


def test_cvss_is_preferred_over_the_coarse_label():
    """The label is present on ~76% of records and the vector on ~95%, and the
    vector is continuous — so a present score must win."""
    assert penalty_units("low", 9.8) == pytest.approx(0.98)
    assert penalty_units("critical", None) == 1.0


# --------------------------------------------------------------------------
# Scoring shape (1.6.0)
# --------------------------------------------------------------------------


def _finding(name="x", severity="high", cvss=None, days=None, direct=True):
    return AdvisoryFinding(
        ecosystem="pypi", name=name, version="1.0", direct=direct, severity=severity,
        cvss_score=cvss, oldest_advisory_days=days, advisory_count=1,
    )


def _score(findings):
    # Published-package scope: the whole closure is scorable there, which is
    # what these shape tests are about.
    adv = DependencyAdvisories(
        collected=True, source="osv", scope="published_package",
        assessed_package="npm:x@1.0.0", assessed_count=500,
        affected_count=len(findings), findings=findings,
    )
    metric = metric_dependency_advisories(_repo_with(adv))
    assert metric is not None
    return metric


def test_volume_still_discriminates_at_high_counts():
    """The 1.5.0 pure-decay formulation collapsed to zero past ~5 findings, so a
    project with 10 advisories scored the same as one with 300. Ordering must
    survive."""
    ten = _score([_finding(f"p{i}", cvss=9.8, direct=False) for i in range(10)]).value
    fifty = _score([_finding(f"p{i}", cvss=9.8, direct=False) for i in range(50)]).value
    assert ten > fifty
    assert fifty >= 1


def test_one_critical_outweighs_many_low_severity_findings():
    """Summed severity alone let a hundred trivial findings equal one critical."""
    one_critical = _score([_finding("bad", cvss=9.8, direct=False)])
    many_low = _score([_finding(f"p{i}", cvss=2.0, direct=False) for i in range(8)])
    direct_of = lambda m: next(c for c in m.components if c.name.startswith("Indirect"))
    assert direct_of(one_critical).points < direct_of(many_low).points


def test_a_long_outstanding_advisory_scores_worse_than_a_fresh_one():
    fresh = _score([_finding(cvss=9.8, days=3)]).value
    old = _score([_finding(cvss=9.8, days=800)]).value
    assert old < fresh


def test_the_outstanding_component_is_excluded_when_no_date_is_known():
    metric = _score([_finding(cvss=9.8, days=None)])
    stale = next(c for c in metric.components if c.name == "No advisories left outstanding")
    assert stale.status == "excluded"
    assert "Remaining weights renormalized" in (metric.note or "")


def test_repository_graph_scope_does_not_score_transitive_findings():
    """The repository graph mixes dev/test pins with shipped dependencies, so
    scoring its transitive set put rails/rails at 16 on the strength of gems
    nobody installs. Only declared runtime dependencies stay scorable."""
    adv = DependencyAdvisories(
        collected=True, source="osv", scope="repository_graph", assessed_count=991,
        affected_count=2,
        findings=[
            _finding("dev-only", cvss=9.8, days=400, direct=False),
            _finding("shipped", cvss=5.0, days=10, direct=True),
        ],
    )
    metric = metric_dependency_advisories(_repo_with(adv))
    assert metric is not None
    indirect = next(c for c in metric.components if c.name.startswith("Indirect"))
    assert indirect.status == "excluded"
    stale = next(c for c in metric.components if c.name == "No advisories left outstanding")
    assert stale.points == stale.max_points


def test_published_scope_still_scores_the_whole_closure():
    adv = DependencyAdvisories(
        collected=True, source="osv", scope="published_package",
        assessed_package="npm:x@1.0.0", assessed_count=50, affected_count=1,
        findings=[_finding("transitive", cvss=9.8, days=400, direct=False)],
    )
    metric = metric_dependency_advisories(_repo_with(adv))
    assert metric is not None
    indirect = next(c for c in metric.components if c.name.startswith("Indirect"))
    assert indirect.status == "partial"
