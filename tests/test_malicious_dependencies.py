"""Malicious dependencies: classification, the split from advisories, and the
red flag it raises.

No network — ``collect_advisories`` runs against an httpx mock transport.
"""

from __future__ import annotations

import httpx
import pytest

from scanner.metrics import (
    MALICIOUS_DEPENDENCY_MULTIPLIER,
    MALICIOUS_DEPENDENCY_OVERALL_CAP,
    compute_metrics,
    metric_dependency_advisories,
    metric_malicious_dependencies,
)
from scanner.models import (
    AllDependencies,
    DependencyAdvisories,
    MaliciousDependency,
    RepoData,
    ResolvedDependency,
)
from scanner.vulns import (
    collect_advisories,
    go_proxy_path,
    is_malicious_id,
    is_malicious_record,
    maven_coordinate_path,
    registry_still_serves,
    summarize,
)


def dep(name, version="1.0.0", ecosystem="npm", direct=True):
    return ResolvedDependency(ecosystem=ecosystem, name=name, version=version, direct=direct)


def _repo_with(advisories: DependencyAdvisories) -> RepoData:
    data = RepoData()
    data.dependencies.all_dependencies = AllDependencies(collected=True)
    data.dependencies.advisories = advisories
    return data


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_mal_identifier_alone_settles_it():
    """The id must be enough: advisory details are capped per scan, so a
    classification that needed the record body would miss malware on a
    repository with a large advisory set."""
    assert is_malicious_id("MAL-2024-1234") is True
    assert is_malicious_id("GHSA-29mw-wpgm-hmr9") is False
    assert is_malicious_id("CVE-2024-1234") is False


def test_records_without_a_mal_id_are_still_recognized():
    origins = {
        "id": "GHSA-xfr4-f89p-2hh3",
        "database_specific": {"malicious-packages-origins": [{"source": "ghsa-malware"}]},
    }
    summary = {"id": "GHSA-xfr4-f89p-2hh3", "summary": "Malicious code in foo (npm)"}
    alias = {"id": "GHSA-xfr4-f89p-2hh3", "aliases": ["MAL-2024-1234"]}
    assert all(is_malicious_record(r) for r in (origins, summary, alias))


def test_an_ordinary_advisory_is_not_malware():
    record = {
        "id": "GHSA-29mw-wpgm-hmr9",
        "summary": "Command Injection in lodash",
        "database_specific": {"severity": "HIGH"},
    }
    assert is_malicious_record(record) is False


# --------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------


def test_malicious_packages_leave_the_advisory_counts():
    packages = [dep("evil"), dep("vulnerable"), dep("clean")]
    results = [
        {"vulns": [{"id": "MAL-2024-1234"}]},
        {"vulns": [{"id": "GHSA-a"}]},
        {},
    ]
    details = {
        "MAL-2024-1234": {"id": "MAL-2024-1234", "published": "2024-04-10T05:55:20Z"},
        "GHSA-a": {"database_specific": {"severity": "HIGH"}},
    }
    summary = summarize(packages, results, details, skipped=0)

    assert summary.malicious_count == 1
    assert [m.name for m in summary.malicious] == ["evil"]
    assert summary.malicious[0].advisory_ids == ["MAL-2024-1234"]
    assert summary.malicious[0].first_reported_at is not None
    # The malicious package is gone from every advisory count, so it cannot be
    # scored twice or diluted into the advisory decay curve.
    assert [f.name for f in summary.findings] == ["vulnerable"]
    assert summary.affected_count == 1
    assert summary.advisory_count == 1


def test_a_package_with_both_kinds_of_record_counts_only_as_malware():
    """Its CVEs are moot once the package itself is malware."""
    results = [{"vulns": [{"id": "MAL-2024-1"}, {"id": "GHSA-b"}]}]
    details = {"GHSA-b": {"database_specific": {"severity": "CRITICAL"}}}
    summary = summarize([dep("evil")], results, details, skipped=0)

    assert summary.malicious_count == 1
    assert summary.findings == []
    assert summary.affected_count == 0


def test_direct_entries_are_listed_first():
    packages = [dep("indirect-evil", direct=False), dep("direct-evil", direct=True)]
    results = [{"vulns": [{"id": "MAL-1"}]}, {"vulns": [{"id": "MAL-2"}]}]
    summary = summarize(packages, results, {}, skipped=0)
    assert [m.name for m in summary.malicious] == ["direct-evil", "indirect-evil"]


def test_malicious_details_are_fetched_ahead_of_ordinary_advisories(monkeypatch):
    """The detail cap must never starve a malicious record of its report date."""
    import scanner.vulns as vulns

    monkeypatch.setattr(vulns, "MAX_DETAIL_LOOKUPS", 1)
    fetched: list[str] = []

    def handler(request):
        if request.url.path == "/v1/querybatch":
            return httpx.Response(
                200,
                json={"results": [{"vulns": [{"id": "GHSA-x"}]}, {"vulns": [{"id": "MAL-9"}]}]},
            )
        if request.url.path.startswith("/v1/vulns/"):
            vuln_id = request.url.path.rsplit("/", 1)[-1]
            fetched.append(vuln_id)
            return httpx.Response(200, json={"id": vuln_id, "published": "2026-01-01T00:00:00Z"})
        # The registry availability probe for the malicious finding.
        return httpx.Response(200)

    warnings: list[str] = []
    summary = collect_advisories(
        [dep("ordinary"), dep("evil")],
        warnings,
        detail_cache={},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert fetched == ["MAL-9"]
    assert summary.malicious[0].first_reported_at is not None


# --------------------------------------------------------------------------
# The metric and the red flag
# --------------------------------------------------------------------------


def _malicious(name="evil", direct=True) -> MaliciousDependency:
    return MaliciousDependency(
        ecosystem="npm", name=name, version="1.0.0", direct=direct, advisory_ids=["MAL-1"]
    )


def test_metric_is_excluded_when_the_lookup_did_not_run():
    assert metric_malicious_dependencies(_repo_with(DependencyAdvisories())) is None


def test_a_clean_graph_scores_full_marks_and_raises_no_flag():
    metric = metric_malicious_dependencies(
        _repo_with(
            DependencyAdvisories(
                collected=True, source="osv", scope="repository_graph", assessed_count=120
            )
        )
    )
    assert metric is not None
    assert metric.value == 100
    assert metric.inputs["red_flag"] is False


def test_one_malicious_package_raises_the_flag():
    metric = metric_malicious_dependencies(
        _repo_with(
            DependencyAdvisories(
                collected=True, source="osv", scope="repository_graph", assessed_count=120,
                malicious_count=1, malicious=[_malicious()],
            )
        )
    )
    assert metric is not None
    assert metric.inputs["red_flag"] is True
    assert metric.value < 100


def test_indirect_malware_is_scored_exactly_like_direct():
    """An install-time payload runs at any depth in the graph."""
    def score(direct: bool) -> int:
        metric = metric_malicious_dependencies(
            _repo_with(
                DependencyAdvisories(
                    collected=True, source="osv", scope="repository_graph", assessed_count=50,
                    malicious_count=1, malicious=[_malicious(direct=direct)],
                )
            )
        )
        assert metric is not None
        return metric.value

    assert score(direct=True) == score(direct=False)


def test_the_flag_caps_security_posture_and_overall():
    data = _repo_with(
        DependencyAdvisories(
            collected=True, source="osv", scope="repository_graph", assessed_count=200,
            malicious_count=1, malicious=[_malicious()],
        )
    )
    metrics = compute_metrics(data)
    assert metrics.overall is not None
    assert metrics.overall.value <= MALICIOUS_DEPENDENCY_OVERALL_CAP
    assert metrics.overall.band == "critical"
    assert metrics.overall.inputs["malicious_dependency_cap"] == MALICIOUS_DEPENDENCY_OVERALL_CAP


def test_the_ceiling_is_reachable_by_the_multiplier():
    """A ceiling the multiplier can never reach is decorative, not a rule.

    At the configured multiplier a top score must land at or above the cap, so
    the cap does the work for strong repositories and the multiplier scales
    everything below them.
    """
    assert 100 * MALICIOUS_DEPENDENCY_MULTIPLIER / 100 >= MALICIOUS_DEPENDENCY_OVERALL_CAP


def test_a_clean_graph_leaves_the_overall_score_untouched():
    data = _repo_with(
        DependencyAdvisories(
            collected=True, source="osv", scope="repository_graph", assessed_count=200
        )
    )
    metrics = compute_metrics(data)
    assert metrics.overall is not None
    assert "malicious_dependency_cap" not in metrics.overall.inputs


def test_a_withdrawn_version_is_reported_but_not_scored():
    """The registry pulled it, so nothing installable remains."""
    metric = metric_malicious_dependencies(
        _repo_with(
            DependencyAdvisories(
                collected=True, source="osv", scope="repository_graph", assessed_count=50,
                malicious_count=1,
                malicious=[
                    MaliciousDependency(
                        ecosystem="npm", name="evil", version="1.0.0", direct=True,
                        advisory_ids=["MAL-1"], still_published=False,
                    )
                ],
            )
        )
    )
    assert metric is not None
    assert metric.inputs["red_flag"] is False
    assert metric.value == 100
    # Still on the record — the dependency is on a compromised name.
    assert metric.inputs["withdrawn_malicious_packages"] == 1
    assert len(metric.inputs["packages"]) == 1


def test_an_unanswered_availability_check_is_scored_as_live():
    """Failing to reach a registry is not evidence that malware was withdrawn."""
    metric = metric_malicious_dependencies(
        _repo_with(
            DependencyAdvisories(
                collected=True, source="osv", scope="repository_graph", assessed_count=50,
                malicious_count=1,
                malicious=[
                    MaliciousDependency(
                        ecosystem="npm", name="evil", version="1.0.0", direct=True,
                        advisory_ids=["MAL-1"], still_published=None,
                    )
                ],
            )
        )
    )
    assert metric is not None
    assert metric.inputs["red_flag"] is True


def test_registry_probe_reads_the_exact_version_not_the_latest():
    """npm leaves an `x.y.z-security` holding package as `latest`. That protects
    everyone resolving a range and nobody who pinned the bad version, so the
    probe must ask for the resolved version itself."""
    asked: list[str] = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert registry_still_serves(client, "npm", "http", "0.0.0", 5.0) is False
    assert asked == ["https://registry.npmjs.org/http/0.0.0"]


def test_uncovered_ecosystem_and_missing_version_are_unanswered():
    """NuGet and Packagist answer with a version *list*, not a status, so they
    are deliberately not probed — see the note beside the URL table."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert registry_still_serves(client, "nuget", "Newtonsoft.Json", "13.0.3", 5.0) is None
    assert registry_still_serves(client, "packagist", "monolog/monolog", "2.9.1", 5.0) is None
    assert registry_still_serves(client, "npm", "evil", None, 5.0) is None


def test_go_module_paths_are_case_encoded_for_the_proxy():
    """Lowercasing instead would 404 every module with a capital letter, which
    reads as "the registry pulled it" and would clear live malware."""
    assert go_proxy_path("github.com/BurntSushi/toml") == "github.com/!burnt!sushi/toml"
    assert go_proxy_path("github.com/pkg/errors") == "github.com/pkg/errors"


def test_maven_coordinates_become_a_repository_path():
    assert maven_coordinate_path("com.google.guava:guava") == "com/google/guava/guava"
    # A coordinate without an artifact half is passed through rather than mangled.
    assert maven_coordinate_path("guava") == "guava"


@pytest.mark.parametrize(
    "ecosystem,name,version,expected_url",
    [
        ("hex", "phoenix", "1.7.10", "https://hex.pm/api/packages/phoenix/releases/1.7.10"),
        ("go", "github.com/BurntSushi/toml", "v1.3.2",
         "https://proxy.golang.org/github.com/!burnt!sushi/toml/@v/v1.3.2.info"),
        ("maven", "com.google.guava:guava", "33.0.0-jre",
         "https://repo1.maven.org/maven2/com/google/guava/guava/33.0.0-jre/"),
    ],
)
def test_newly_covered_ecosystems_ask_the_right_url(ecosystem, name, version, expected_url):
    asked: list[str] = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert registry_still_serves(client, ecosystem, name, version, 5.0) is False
    assert asked == [expected_url]


def test_malware_is_not_also_scored_as_an_advisory():
    """The advisory metric must see a clean set — the package left it."""
    adv = DependencyAdvisories(
        collected=True, source="osv", scope="repository_graph", assessed_count=100,
        malicious_count=1, malicious=[_malicious()],
    )
    metric = metric_dependency_advisories(_repo_with(adv))
    assert metric is not None
    assert metric.value == 100
