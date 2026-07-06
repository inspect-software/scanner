from scanner.collect import TreeEntry, _ai_readiness
from scanner.metrics import (
    compute_metrics,
    metric_ai_agent_context,
    metric_ai_code_legibility,
    metric_ai_interfaces,
    metric_ai_verify_loop,
)
from scanner.models import (
    AIReadinessSignals,
    CommunityHealth,
    Dependency,
    Popularity,
    QualitySignals,
    RepoData,
    RepoInfo,
    SecuritySignals,
)


def _entries(*paths_or_pairs):
    """Build TreeEntry list from paths (size 0) or (path, size) pairs."""
    out = []
    for item in paths_or_pairs:
        if isinstance(item, tuple):
            out.append(TreeEntry(path=item[0], size=item[1]))
        else:
            out.append(TreeEntry(path=item, size=0))
    return out


# --- collector ----------------------------------------------------------------


def test_collector_detects_agent_and_infra_signals():
    entries = _entries(
        ("CLAUDE.md", 1200),
        (".github/copilot-instructions.md", 400),
        "llms.txt",
        "Makefile",
        "pyrightconfig.json",
        ".devcontainer/devcontainer.json",
        "Dockerfile",
        "flake.nix",
        "openapi.yaml",
        "examples/quickstart.py",
        ("src/app/core.py", 1000),
    )
    ai = _ai_readiness(entries, [])
    assert ai.agent_instruction_files == [".github/copilot-instructions.md", "CLAUDE.md"]
    assert ai.agent_instruction_max_bytes == 1200
    assert ai.has_llms_txt
    assert ai.bootstrap_files == ["Makefile"]
    assert ai.typecheck_configs == ["pyrightconfig.json"]
    assert ai.has_devcontainer and ai.has_dockerfile and ai.has_nix
    assert ai.api_schema_files == ["openapi.yaml"]
    assert ai.example_dirs == ["examples"]
    assert ai.source_files_sampled == 2  # core.py + examples/quickstart.py


def test_collector_mcp_from_dependency():
    ai = _ai_readiness(_entries("src/main.py"), [Dependency(ecosystem="pypi", name="mcp", manifest="pyproject.toml")])
    assert ai.has_mcp_signal


def test_collector_excludes_vendored_and_flags_oversized():
    entries = _entries(
        ("node_modules/lib/index.js", 999_999),  # vendored, ignored
        ("src/huge.py", 90_000),                  # oversized source
        ("src/small.py", 500),
    )
    ai = _ai_readiness(entries, [])
    assert ai.source_files_sampled == 2
    assert ai.oversized_source_files == 1
    assert ai.largest_source_bytes == 90_000


# --- metrics ------------------------------------------------------------------


def test_agent_context_stub_scores_below_substantive():
    stub = RepoData(ai_readiness=AIReadinessSignals(
        agent_instruction_files=["CLAUDE.md"], agent_instruction_max_bytes=50))
    full = RepoData(ai_readiness=AIReadinessSignals(
        agent_instruction_files=["CLAUDE.md"], agent_instruction_max_bytes=3000, has_llms_txt=True))
    ms, mf = metric_ai_agent_context(stub), metric_ai_agent_context(full)
    assert ms.value < mf.value
    by = {c.name: c for c in ms.components}
    assert by["Agent instructions"].status == "partial"  # stub


def test_verify_loop_reuses_quality_and_typed_language():
    data = RepoData(
        repo=RepoInfo(primary_language="Go"),
        quality_signals=QualitySignals(has_tests=True, has_linter_config=True),
        security_signals=SecuritySignals(lockfiles=["go.sum"]),
        ai_readiness=AIReadinessSignals(bootstrap_files=["Makefile"]),
    )
    m = metric_ai_verify_loop(data)
    by = {c.name: c for c in m.components}
    assert by["Automated tests"].status == "met"
    assert by["Static type checking"].status == "met"  # Go is statically typed
    assert by["Reproducible environment"].status == "met"  # lockfile
    assert m.band in ("good", "excellent")


def test_code_legibility_none_without_source():
    assert metric_ai_code_legibility(RepoData()) is None


def test_code_legibility_penalizes_oversized_files():
    clean = RepoData(repo=RepoInfo(primary_language="Python"),
                     ai_readiness=AIReadinessSignals(source_files_sampled=100, oversized_source_files=0))
    bloated = RepoData(repo=RepoInfo(primary_language="Python"),
                       ai_readiness=AIReadinessSignals(source_files_sampled=100, oversized_source_files=60))
    assert metric_ai_code_legibility(clean).value > metric_ai_code_legibility(bloated).value


def test_interfaces_none_when_no_interface():
    assert metric_ai_interfaces(RepoData()) is None
    data = RepoData(ai_readiness=AIReadinessSignals(example_dirs=["examples"]))
    assert metric_ai_interfaces(data) is not None


# --- category is independent (weight 0.0) -------------------------------------


def test_ai_readiness_category_does_not_move_overall():
    base = dict(popularity=Popularity(stars=500), community=CommunityHealth(has_readme=True))
    without = compute_metrics(RepoData(**base))
    with_ai = compute_metrics(RepoData(
        **base,
        ai_readiness=AIReadinessSignals(
            agent_instruction_files=["CLAUDE.md"], agent_instruction_max_bytes=5000,
            has_llms_txt=True, bootstrap_files=["Makefile"]),
    ))
    # AI Readiness is surfaced as a category...
    assert with_ai.category("ai_readiness") is not None
    assert with_ai.category("ai_readiness").weight == 0.0
    # ...but a fully-loaded AI signal set leaves the overall health score identical.
    assert with_ai.overall.value == without.overall.value
