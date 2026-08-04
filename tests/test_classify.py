from scanner.classify import (
    artifact_signals,
    classify,
    declaration_tokens,
    structure_tokens,
)
from scanner.models import (
    AIReadinessSignals,
    ArtifactSignals,
    Dependency,
    DependencySignals,
    EcosystemData,
    EcosystemPackage,
    ManifestDeclaration,
    RepoData,
    RepoInfo,
)


# --- manifest declarations ----------------------------------------------------


def test_package_json_bin_declares_a_command():
    tokens = declaration_tokens("package.json", '{"name": "x", "bin": {"x": "./cli.js"}}')
    assert tokens == ["npm.bin"]


def test_package_json_reports_both_a_command_and_an_entry_point():
    tokens = declaration_tokens(
        "package.json", '{"name": "esbuild", "bin": "./bin/esbuild", "main": "./lib/main.js"}'
    )
    assert tokens == ["npm.bin", "npm.entry"]


def test_package_json_private_is_recorded():
    assert "npm.private" in declaration_tokens("package.json", '{"private": true}')


def test_pyproject_console_scripts_and_plugin_entry_points():
    text = """
[project]
name = "pytest-cov"
scripts = {covctl = "pytest_cov.cli:main"}
[project.entry-points.pytest11]
cov = "pytest_cov.plugin"
"""
    tokens = declaration_tokens("pyproject.toml", text)
    assert "pypi.console_scripts" in tokens
    assert "pypi.entry_point:pytest11" in tokens


def test_pyproject_classifiers_collapse_to_their_branch():
    text = """
[project]
name = "app"
classifiers = [
  "Environment :: X11 Applications :: Qt",
  "Topic :: Software Development :: Libraries :: Python Modules",
  "Programming Language :: Python :: 3",
]
"""
    tokens = declaration_tokens("pyproject.toml", text)
    assert "pypi.classifier:Environment :: X11 Applications" in tokens
    assert "pypi.classifier:Topic :: Software Development :: Libraries" in tokens
    # Subject-matter classifiers say nothing about how the software is used.
    assert not any("Programming Language" in token for token in tokens)


def test_poetry_scripts_are_console_scripts():
    text = '[tool.poetry]\nname = "x"\n[tool.poetry.scripts]\nx = "x:main"\n'
    assert "pypi.console_scripts" in declaration_tokens("pyproject.toml", text)


def test_setup_py_is_read_by_pattern():
    text = "setup(name='x', entry_points={'console_scripts': ['x=x:main']})"
    assert "pypi.console_scripts" in declaration_tokens("setup.py", text)


def test_cargo_binary_and_library_targets():
    text = '[package]\nname = "ripgrep"\n[[bin]]\nname = "rg"\n[lib]\nname = "grep"\n'
    tokens = declaration_tokens("Cargo.toml", text)
    assert "cargo.bin" in tokens and "cargo.lib" in tokens


def test_cargo_publish_false_is_recorded():
    text = '[package]\nname = "internal"\npublish = false\n'
    assert "cargo.publish_false" in declaration_tokens("Cargo.toml", text)


def test_cargo_categories_are_lowercased_tokens():
    text = '[package]\nname = "x"\ncategories = ["Command-Line-Utilities"]\n'
    assert "cargo.category:command-line-utilities" in declaration_tokens("Cargo.toml", text)


def test_composer_type_is_taken_verbatim():
    tokens = declaration_tokens("composer.json", '{"type": "wordpress-plugin"}')
    assert tokens == ["composer.type:wordpress-plugin"]


def test_csproj_output_type_and_tool_packaging():
    text = (
        '<Project Sdk="Microsoft.NET.Sdk">'
        "<PropertyGroup><OutputType>Exe</OutputType>"
        "<PackAsTool>true</PackAsTool>"
        "<TargetFramework>net8.0-windows</TargetFramework></PropertyGroup></Project>"
    )
    tokens = declaration_tokens("Tool.csproj", text)
    assert "nuget.output_type:exe" in tokens
    assert "nuget.pack_as_tool" in tokens
    assert "nuget.platform_tfm:windows" in tokens


def test_csproj_web_sdk():
    text = '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>'
    assert declaration_tokens("Api.csproj", text) == ["nuget.sdk:web"]


def test_pom_packaging():
    text = (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<packaging>war</packaging></project>"
    )
    assert declaration_tokens("pom.xml", text) == ["maven.packaging:war"]


def test_gemspec_executables():
    text = "Gem::Specification.new do |s|\n  s.executables = ['rubocop']\nend"
    tokens = declaration_tokens("rubocop.gemspec", text)
    assert tokens == ["gem.gemspec", "gem.executables"]


def test_mix_escript_and_releases():
    text = "def project do\n  [escript: [main_module: X], releases: [x: []]]\nend"
    tokens = declaration_tokens("mix.exs", text)
    assert "mix.escript" in tokens and "mix.releases" in tokens


def test_unparseable_manifest_declares_nothing():
    assert declaration_tokens("package.json", "not json at all") == []


def test_go_mod_declares_nothing_here():
    assert declaration_tokens("go.mod", "module github.com/x/y\n") == []


# --- file-tree structure ------------------------------------------------------


def test_go_command_directory_is_a_binary():
    assert "tree.go_main" in structure_tokens(["cmd/serve/main.go", "go.mod"])


def test_any_go_source_in_a_command_directory_counts():
    """go-critic's second binary is cmd/go-critic-analysis/go-critic-analysis.go."""
    assert "tree.go_main" in structure_tokens(["cmd/tool/tool.go"])


def test_go_packages_outside_cmd_and_internal_are_importable():
    assert "tree.go_importable" in structure_tokens(["checkers/checker.go", "go.mod"])


def test_go_internal_packages_are_sealed_by_the_compiler():
    tokens = structure_tokens(["internal/app/app.go", "cmd/x/main.go"])
    assert "tree.go_importable" not in tokens


def test_go_root_sources_beside_main_go_are_package_main():
    tokens = structure_tokens(["main.go", "helpers.go"])
    assert "tree.go_importable" not in tokens


def test_go_root_sources_without_main_go_are_importable():
    assert "tree.go_importable" in structure_tokens(["gin.go", "context.go"])


def test_compose_and_kubernetes_are_service_shaped():
    tokens = structure_tokens(["docker-compose.yml", "k8s/deployment.yaml"])
    assert "tree.compose" in tokens and "tree.k8s" in tokens


def test_tauri_config_is_a_desktop_app():
    assert "tree.tauri" in structure_tokens(["src-tauri/tauri.conf.json"])


def test_browser_extension_needs_a_manifest_and_a_companion():
    assert "tree.browser_extension" in structure_tokens(["manifest.json", "background.js"])
    # A bare manifest.json is a config file in half the ecosystems there are.
    assert "tree.browser_extension" not in structure_tokens(["manifest.json"])


def test_example_directories_are_not_the_product():
    assert structure_tokens(["examples/server/docker-compose.yml"]) == []


def test_artifact_signals_pairs_manifests_with_their_ecosystem():
    signals = artifact_signals(
        {"composer.json": '{"type": "library"}', "README.md": "hi"},
        ["composer.json", "config.ru"],
    )
    assert signals.collected is True
    assert [(d.path, d.ecosystem) for d in signals.declarations] == [
        ("composer.json", "packagist")
    ]
    assert signals.structure == ["tree.config_ru"]


# --- classification -----------------------------------------------------------


def _data(**kwargs) -> RepoData:
    return RepoData(**kwargs)


def _declared(path: str, ecosystem: str, *tokens: str) -> ArtifactSignals:
    return ArtifactSignals(
        collected=True,
        declarations=[ManifestDeclaration(path=path, ecosystem=ecosystem, tokens=list(tokens))],
    )


def test_a_command_without_an_entry_point_is_not_a_library():
    result = classify(_data(artifacts=_declared("package.json", "npm", "npm.bin")))
    assert result.labels == ["cli"]
    assert result.runs_as_process is True
    assert result.consumed_by_code is False
    assert result.confidence == "high"


def test_a_hybrid_carries_both_labels_and_both_obligations():
    """ripgrep is a crate and a binary. Both are true; neither cancels the other.
    The binary target names no interface, so the executable side is the bare
    parent until something else says "command-line"."""
    result = classify(
        _data(artifacts=_declared("Cargo.toml", "crates", "cargo.bin", "cargo.lib"))
    )
    assert set(result.labels) == {"application", "library"}
    assert result.runs_as_process is True
    assert result.consumed_by_code is True


def test_the_go_module_proxy_is_not_publication():
    """Proxy indexing is automatic; alone it cannot carry the library label."""
    data = _data(
        ecosystem=EcosystemData(
            packages=[
                EcosystemPackage(
                    ecosystem="go",
                    name="github.com/x/app",
                    registry_url="https://pkg.go.dev/github.com/x/app",
                )
            ]
        )
    )
    result = classify(data)
    assert result.labels == []
    assert result.confidence == "none"


def test_a_go_module_with_importable_packages_is_a_library():
    data = _data(
        ecosystem=EcosystemData(
            packages=[
                EcosystemPackage(
                    ecosystem="go",
                    name="github.com/gin-gonic/gin",
                    registry_url="https://pkg.go.dev/github.com/gin-gonic/gin",
                )
            ]
        ),
        artifacts=ArtifactSignals(collected=True, structure=["tree.go_importable"]),
    )
    result = classify(data)
    assert result.labels == ["library"]
    assert result.consumed_by_code is True


def test_a_go_linter_reads_as_a_command_not_a_library():
    """The go-critic case: cmd/ directory, linter topic, linter description —
    and a proxy entry that used to be the only thing that scored."""
    data = _data(
        ecosystem=EcosystemData(
            packages=[
                EcosystemPackage(
                    ecosystem="go",
                    name="github.com/go-critic/go-critic",
                    registry_url="https://pkg.go.dev/github.com/go-critic/go-critic",
                )
            ]
        ),
        artifacts=ArtifactSignals(collected=True, structure=["tree.go_main"]),
        repo=RepoInfo(
            topics=["linter", "golang", "style-checker"],
            description="The most opinionated Go source code linter for code audit.",
        ),
    )
    result = classify(data)
    assert result.primary == "cli"
    assert "library" not in result.labels


def test_a_go_hybrid_carries_both_readings():
    """With importable packages beside cmd/, both labels hold — golangci-lint
    imports go-critic's checkers while go-critic ships its own binary."""
    data = _data(
        ecosystem=EcosystemData(
            packages=[
                EcosystemPackage(
                    ecosystem="go",
                    name="github.com/go-critic/go-critic",
                    registry_url="https://pkg.go.dev/github.com/go-critic/go-critic",
                )
            ]
        ),
        artifacts=ArtifactSignals(
            collected=True, structure=["tree.go_importable", "tree.go_main"]
        ),
        repo=RepoInfo(topics=["linter"], description="Go source code linter"),
    )
    result = classify(data)
    assert set(result.labels) == {"cli", "library"}
    assert result.primary == "cli"
    assert set(result.top) == {"application", "library"}


def test_evidence_may_stop_at_the_parent():
    """A Cargo binary target proves an executable, not what kind: the answer
    is the bare parent, not a guessed subtype."""
    data = _data(artifacts=_declared("Cargo.toml", "crates", "cargo.bin"))
    result = classify(data)
    assert result.labels == ["application"]
    assert result.top == ["application"]
    assert result.runs_as_process is True
    assert result.primary == "application"
    assert result.confidence == "high"


def test_a_subtype_absorbs_its_parent_in_the_label_list():
    """When the subtype is known, the bare parent is not shown beside it."""
    data = _data(
        artifacts=_declared("Cargo.toml", "crates", "cargo.bin"),
        repo=RepoInfo(topics=["cli", "command-line-tool"]),
    )
    result = classify(data)
    assert result.labels == ["cli"]
    assert result.top == ["application"]


def test_a_linter_tag_on_a_host_plugin_is_the_hosts_tool():
    """An ESLint rule pack carries `linter` too; the runnable tool is ESLint."""
    data = _data(
        repo=RepoInfo(
            topics=["eslint-plugin", "linter"],
            description="ESLint plugin with rules that help validate proper imports.",
        )
    )
    result = classify(data)
    assert "plugin" in result.labels
    assert "cli" not in result.labels
    assert all(e.source not in ("tag:linter", "description:tool") for e in result.evidence)


def test_a_standalone_formatter_classifies_from_tag_and_description():
    data = _data(repo=RepoInfo(topics=["formatter"], description="An opinionated code formatter"))
    result = classify(data)
    assert result.labels == ["cli"]
    assert result.confidence == "low"


def test_publishing_makes_a_repository_a_library_by_default():
    data = _data(
        ecosystem=EcosystemData(
            packages=[
                EcosystemPackage(
                    ecosystem="pypi", name="httpx", registry_url="https://pypi.org/p/httpx"
                )
            ]
        )
    )
    result = classify(data)
    assert result.labels == ["library"]
    assert result.consumed_by_code is True


def test_a_dotnet_tool_is_ruled_out_of_being_a_library():
    data = _data(
        artifacts=_declared("T.csproj", "nuget", "nuget.pack_as_tool", "nuget.output_type:exe"),
        ecosystem=EcosystemData(
            packages=[
                EcosystemPackage(
                    ecosystem="nuget", name="t", registry_url="https://nuget.org/t"
                )
            ]
        ),
    )
    result = classify(data)
    assert "cli" in result.labels
    assert "library" not in result.labels
    assert result.consumed_by_code is False


def test_a_composer_project_is_not_a_dependency():
    data = _data(
        artifacts=_declared("composer.json", "packagist", "composer.type:project"),
        dependencies=DependencySignals(
            dependencies=[
                Dependency(ecosystem="packagist", name="laravel/framework", manifest="composer.json")
            ]
        ),
    )
    result = classify(data)
    assert result.labels == ["network-service"]
    assert result.consumed_by_code is False


def test_registry_declared_type_classifies_a_plugin():
    data = _data(
        ecosystem=EcosystemData(
            packages=[
                EcosystemPackage(
                    ecosystem="packagist",
                    name="x/y",
                    registry_url="https://packagist.org/packages/x/y",
                    declared_type="wordpress-plugin",
                )
            ]
        )
    )
    result = classify(data)
    assert "plugin" in result.labels
    assert result.host_extension is True


def test_crates_categories_are_trusted_as_a_controlled_vocabulary():
    data = _data(
        ecosystem=EcosystemData(
            packages=[
                EcosystemPackage(
                    ecosystem="crates",
                    name="ripgrep",
                    registry_url="https://crates.io/crates/ripgrep",
                    categories=["command-line-utilities"],
                )
            ]
        )
    )
    assert "cli" in classify(data).labels


def test_a_single_topic_is_never_enough():
    result = classify(_data(repo=RepoInfo(topics=["cli"])))
    assert result.labels == []
    assert result.confidence == "none"


def test_two_weak_signals_agreeing_are_enough():
    data = _data(repo=RepoInfo(topics=["cli"], description="A CLI for tidying imports"))
    result = classify(data)
    assert result.labels == ["cli"]
    assert result.confidence == "low"


def test_a_dependency_fingerprint_classifies_a_service():
    data = _data(
        dependencies=DependencySignals(
            dependencies=[Dependency(ecosystem="crates", name="axum", manifest="Cargo.toml")]
        )
    )
    assert classify(data).labels == ["network-service"]


def test_an_mcp_signal_alone_does_not_make_a_repository_an_mcp_server():
    """`.mcp.json` configures an editor to *call* servers; it does not write one."""
    data = _data(ai_readiness=AIReadinessSignals(has_mcp_signal=True))
    assert classify(data).labels == []


def test_an_mcp_signal_with_corroboration_does():
    data = _data(
        ai_readiness=AIReadinessSignals(has_mcp_signal=True),
        repo=RepoInfo(topics=["mcp-server"]),
    )
    result = classify(data)
    assert result.labels == ["mcp-server"]
    assert result.runs_as_process is True


def test_nothing_observed_answers_nothing():
    result = classify(_data())
    assert result.labels == []
    assert result.primary is None
    assert result.confidence == "none"
    assert (result.consumed_by_code, result.runs_as_process, result.host_extension) == (
        False,
        False,
        False,
    )


def test_a_report_written_before_the_artifact_scan_still_classifies():
    """Old reports carry no declarations — they must degrade, not misfire."""
    data = _data(
        repo=RepoInfo(topics=["http-server", "self-hosted"]),
        dependencies=DependencySignals(
            dependencies=[Dependency(ecosystem="npm", name="express", manifest="package.json")]
        ),
    )
    result = classify(data)
    assert result.artifacts == []
    assert result.labels == ["network-service"]
    assert result.confidence == "medium"


def test_a_monorepo_counts_the_same_observation_once():
    """Twenty package.json files are one observation, not twenty."""
    many = ArtifactSignals(
        collected=True,
        declarations=[
            ManifestDeclaration(
                path=f"packages/p{i}/package.json", ecosystem="npm", tokens=["npm.entry"]
            )
            for i in range(20)
        ],
    )
    result = classify(_data(artifacts=many))
    # One `npm.entry`, discounted once for sitting below the root.
    assert result.scores["library"] == 4.8


def test_a_monorepo_reports_each_artifact_separately():
    signals = ArtifactSignals(
        collected=True,
        declarations=[
            ManifestDeclaration(path="api/pom.xml", ecosystem="maven", tokens=["maven.packaging:war"]),
            ManifestDeclaration(path="sdk/pom.xml", ecosystem="maven", tokens=["maven.packaging:jar"]),
        ],
    )
    result = classify(_data(artifacts=signals))
    by_path = {a.path: a.labels for a in result.artifacts}
    assert by_path["api/pom.xml"] == ["network-service"]
    assert by_path["sdk/pom.xml"] == ["library"]


def test_evidence_records_what_ruled_a_label_out():
    result = classify(_data(artifacts=_declared("package.json", "npm", "npm.bin", "npm.private")))
    negative = [e for e in result.evidence if e.weight < 0]
    assert [(e.label, e.source) for e in negative] == [("library", "npm.private")]


def test_a_private_packages_command_is_repository_tooling():
    """A build script wired up in an unpublished package.json is not the product."""
    signals = _declared("package.json", "npm", "npm.private", "npm.bin", "npm.oclif")
    assert classify(_data(artifacts=signals)).labels == []


def test_the_root_manifest_outweighs_one_buried_in_the_tree():
    signals = ArtifactSignals(
        collected=True,
        declarations=[
            ManifestDeclaration(
                path="composer.json", ecosystem="packagist", tokens=["composer.type:wordpress-plugin"]
            ),
            ManifestDeclaration(
                path="tools/release/package.json", ecosystem="npm", tokens=["npm.bin"]
            ),
        ],
    )
    result = classify(_data(artifacts=signals))
    assert result.primary == "plugin"
    assert set(result.labels) == {"plugin", "cli"}


def test_a_repository_depending_on_itself_proves_nothing():
    signals = ArtifactSignals(
        collected=True,
        declarations=[
            ManifestDeclaration(
                path="sinatra.gemspec", ecosystem="rubygems", name="sinatra", tokens=["gem.gemspec"]
            )
        ],
    )
    data = _data(
        artifacts=signals,
        dependencies=DependencySignals(
            dependencies=[Dependency(ecosystem="rubygems", name="sinatra", manifest="Gemfile")]
        ),
    )
    result = classify(data)
    assert result.labels == ["library"]
    assert "network-service" not in result.labels


def test_every_label_a_rule_emits_is_fully_declared():
    """A new label must be added to the schema, the ordering, and a roll-up.

    Missing the ordering raises at classification time; missing a roll-up fails
    silently, which is worse — the label would be reported and then bind
    nothing.
    """
    import typing

    from scanner import classify as c

    used = {label for _, label in c.DESCRIPTION_RULES}
    used |= set(c.DEPENDENCY_RULES.values()) | set(c.TAG_RULES.values())
    used |= {label for label, _ in c.ENTRY_POINT_RULE}
    for table in (
        c.DECLARED_RULES,
        c.STRUCTURE_RULES,
        c.REGISTRY_TYPE_RULES,
        c.REGISTRY_CATEGORY_RULES,
    ):
        for rules in table.values():
            used |= {label for label, _ in rules}

    known = c.TOP_LABELS | set(c.SUBTYPE_PARENT)
    assert used <= known, f"rules emit labels outside the tree: {used - known}"
    assert used <= set(c.SURFACE_ORDER)
    # The tree itself must be closed: every subtype's parent is a top label,
    # and the ordering knows every label that can appear.
    assert set(c.SUBTYPE_PARENT.values()) <= c.TOP_LABELS
    assert known <= set(c.SURFACE_ORDER)


def test_primary_is_the_best_supported_label():
    data = _data(
        artifacts=_declared("composer.json", "packagist", "composer.type:library"),
        repo=RepoInfo(topics=["cli", "command-line-tool"]),
    )
    result = classify(data)
    assert result.primary == "library"
    assert set(result.labels) == {"library", "cli"}
