from scanner.ecosystems import (
    collect_dependencies,
    identify_packages,
    manifest_paths,
    map_crates,
    map_npm,
    map_packagist,
    map_pypi,
    parse_cargo_toml,
    parse_cargo_toml_dependencies,
    parse_composer_json,
    parse_composer_json_dependencies,
    parse_package_json,
    parse_package_json_dependencies,
    parse_pyproject,
    parse_pyproject_dependencies,
    parse_setup_cfg,
    parse_setup_cfg_dependencies,
)


# --- manifest parsing ---------------------------------------------------------


def test_parse_pyproject_pep621():
    assert parse_pyproject('[project]\nname = "flask"\nversion = "3"\n') == "flask"


def test_parse_pyproject_poetry():
    assert parse_pyproject('[tool.poetry]\nname = "httpx"\n') == "httpx"


def test_parse_setup_cfg():
    assert parse_setup_cfg("[metadata]\nname = requests\n") == "requests"


def test_parse_package_json():
    assert parse_package_json('{"name": "express", "version": "5"}') == "express"


def test_parse_package_json_private_skipped():
    assert parse_package_json('{"name": "secret", "private": true}') is None


def test_parse_composer_json():
    assert parse_composer_json('{"name": "monolog/monolog"}') == "monolog/monolog"


def test_parse_cargo_toml():
    assert parse_cargo_toml('[package]\nname = "serde"\nversion = "1"\n') == "serde"


def test_identify_packages_dedup_and_map():
    manifests = {
        "pyproject.toml": '[project]\nname = "flask"\n',
        "package.json": '{"name": "flask-ui"}',
        "sub/Cargo.toml": '[package]\nname = "flaskrs"\n',
        "broken.txt": "ignored",
    }
    found = identify_packages(manifests)
    assert ("pypi", "flask") in found
    assert ("npm", "flask-ui") in found
    assert ("crates", "flaskrs") in found


def test_identify_ignores_malformed():
    assert identify_packages({"pyproject.toml": "not valid toml ["}) == []


def test_manifest_paths_depth_limit():
    tree = ["pyproject.toml", "sub/package.json", "a/b/c/Cargo.toml", "README.md"]
    paths = manifest_paths(tree)
    assert "pyproject.toml" in paths
    assert "sub/package.json" in paths
    assert "a/b/c/Cargo.toml" not in paths  # too deep


# --- declared dependency parsing (pure) ---------------------------------------


def test_parse_pyproject_dependencies_pep621():
    text = (
        '[project]\nname = "flask"\ndependencies = [\n'
        '  "Werkzeug>=3.0",\n'
        '  "click ; python_version>=\'3.8\'",\n'
        '  "requests[security]>=2.0,<3",\n'
        "]\n"
    )
    deps = parse_pyproject_dependencies(text)
    assert deps == [
        ("Werkzeug", ">=3.0"),
        ("click", None),
        ("requests", ">=2.0,<3"),
    ]


def test_parse_pyproject_dependencies_poetry_excludes_python_and_dev_group():
    text = (
        "[tool.poetry]\nname = \"gito.bot\"\n"
        "[tool.poetry.dependencies]\n"
        'python = "^3.11"\n'
        'ai-microcore = "~6.4.4"\n'
        'anthropic = ">=0.57.1,<1"\n'
        "[tool.poetry.group.dev.dependencies]\n"
        'flake8 = "*"\n'
    )
    deps = parse_pyproject_dependencies(text)
    assert ("ai-microcore", "~6.4.4") in deps
    assert ("anthropic", ">=0.57.1,<1") in deps
    assert not any(name == "python" for name, _ in deps)
    assert not any(name == "flake8" for name, _ in deps)


def test_parse_pyproject_dependencies_poetry_table_spec():
    text = (
        '[tool.poetry]\nname = "x"\n[tool.poetry.dependencies]\n'
        'requests = { version = "^2.0", extras = ["security"] }\n'
    )
    assert parse_pyproject_dependencies(text) == [("requests", "^2.0")]


def test_parse_setup_cfg_dependencies():
    text = "[options]\ninstall_requires =\n    requests>=2.0\n    click\n"
    assert parse_setup_cfg_dependencies(text) == [("requests", ">=2.0"), ("click", None)]


def test_parse_setup_cfg_dependencies_absent():
    assert parse_setup_cfg_dependencies("[metadata]\nname = x\n") == []


def test_parse_package_json_dependencies_excludes_dev():
    text = (
        '{"dependencies": {"express": "^4.18.0"}, '
        '"devDependencies": {"jest": "*"}}'
    )
    assert parse_package_json_dependencies(text) == [("express", "^4.18.0")]


def test_parse_composer_json_dependencies_excludes_platform_packages():
    text = '{"require": {"php": "^8.0", "ext-json": "*", "monolog/monolog": "^3.0"}}'
    assert parse_composer_json_dependencies(text) == [("monolog/monolog", "^3.0")]


def test_parse_cargo_toml_dependencies_string_and_table_spec():
    text = (
        "[dependencies]\n"
        'serde = "1.0"\n'
        'tokio = { version = "1", features = ["full"] }\n'
    )
    assert parse_cargo_toml_dependencies(text) == [("serde", "1.0"), ("tokio", "1")]


def test_collect_dependencies_across_manifests():
    texts = {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["requests>=2.0"]\n',
        "sub/package.json": '{"dependencies": {"lodash": "~4.17.21"}}',
        "unsupported.txt": "irrelevant",
    }
    deps = collect_dependencies(texts)
    by_name = {d.name: d for d in deps}
    assert by_name["requests"].ecosystem == "pypi"
    assert by_name["requests"].version_constraint == ">=2.0"
    assert by_name["requests"].manifest == "pyproject.toml"
    assert by_name["lodash"].ecosystem == "npm"
    assert by_name["lodash"].manifest == "sub/package.json"


def test_collect_dependencies_malformed_manifest_ignored():
    assert collect_dependencies({"pyproject.toml": "not valid toml ["}) == []


# --- response mapping (pure) --------------------------------------------------


def test_map_pypi():
    payload = {
        "info": {"version": "3.1.0", "license_expression": "BSD-3-Clause",
                 "project_urls": {"Source": "https://github.com/pallets/flask"}},
        "releases": {
            "3.0.0": [{"upload_time_iso_8601": "2023-09-30T00:00:00Z", "yanked": False}],
            "3.1.0": [{"upload_time_iso_8601": "2025-01-01T00:00:00Z", "yanked": False}],
        },
    }
    pkg = map_pypi("flask", payload, 5_000_000, "pallets/flask")
    assert pkg.ecosystem == "pypi"
    assert pkg.latest_version == "3.1.0"
    assert pkg.versions_count == 2
    assert pkg.monthly_downloads == 5_000_000
    assert pkg.license == "BSD-3-Clause"
    assert pkg.matches_repo is True


def test_map_npm_deprecated_and_match():
    payload = {
        "dist-tags": {"latest": "2.0.0"},
        "versions": {"1.0.0": {}, "2.0.0": {"deprecated": "use v3", "license": "MIT"}},
        "time": {"created": "2020-01-01T00:00:00Z", "2.0.0": "2024-06-01T00:00:00Z"},
        "maintainers": [{"name": "a"}, {"name": "b"}],
        "repository": {"url": "git+https://github.com/other/repo.git"},
    }
    pkg = map_npm("thing", payload, 1000, "me/mine")
    assert pkg.is_deprecated is True
    assert pkg.deprecation_note == "use v3"
    assert pkg.maintainers_count == 2
    assert pkg.matches_repo is False  # repo points elsewhere


def test_map_packagist_abandoned():
    payload = {"package": {
        "versions": {
            "2.0.0": {"time": "2024-01-01T00:00:00Z", "license": ["MIT"]},
            "dev-main": {"time": "2024-02-01T00:00:00Z"},
        },
        "downloads": {"monthly": 12345, "total": 999999},
        "abandoned": "monolog/monolog",
        "repository": "https://github.com/acme/log",
    }}
    pkg = map_packagist("acme/log", payload, "acme/log")
    assert pkg.is_deprecated is True
    assert pkg.deprecation_note == "monolog/monolog"
    assert pkg.monthly_downloads == 12345
    assert pkg.matches_repo is True


def test_map_crates_monthly_approximation():
    payload = {
        "crate": {"max_stable_version": "1.0.0", "downloads": 900, "recent_downloads": 900,
                  "repository": "https://github.com/serde-rs/serde"},
        "versions": [{"num": "1.0.0", "created_at": "2024-01-01T00:00:00Z",
                      "license": "MIT OR Apache-2.0", "yanked": False}],
    }
    pkg = map_crates("serde", payload, "serde-rs/serde")
    assert pkg.monthly_downloads == 300  # 900 / 3
    assert pkg.total_downloads == 900
    assert pkg.license == "MIT OR Apache-2.0"
    assert pkg.matches_repo is True


# --- extended ecosystems: dependency parsing ---------------------------------

from scanner.ecosystems import (  # noqa: E402
    ecosystem_for_manifest,
    map_hex,
    map_rubygems,
    parse_csproj_dependencies,
    parse_gemfile_dependencies,
    parse_gemspec,
    parse_go_mod_dependencies,
    parse_mix_dependencies,
    parse_mix_exs,
    parse_pom_dependencies,
)


def test_ecosystem_for_manifest_exact_and_suffix():
    assert ecosystem_for_manifest("go.mod") == "go"
    assert ecosystem_for_manifest("pom.xml") == "maven"
    assert ecosystem_for_manifest("Gemfile") == "rubygems"
    assert ecosystem_for_manifest("mix.exs") == "hex"
    assert ecosystem_for_manifest("MyApp.csproj") == "nuget"
    assert ecosystem_for_manifest("foo.gemspec") == "rubygems"
    assert ecosystem_for_manifest("README.md") is None


def test_parse_go_mod_dependencies_skips_indirect():
    go = (
        "module github.com/acme/tool\ngo 1.21\n"
        "require (\n"
        "    github.com/spf13/cobra v1.8.0\n"
        "    github.com/x/y v1.0.0 // indirect\n"
        ")\n"
        "require github.com/pkg/errors v0.9.1\n"
    )
    deps = parse_go_mod_dependencies(go)
    assert ("github.com/spf13/cobra", "v1.8.0") in deps
    assert ("github.com/pkg/errors", "v0.9.1") in deps
    assert all("y" != n.rsplit("/", 1)[-1] for n, _ in deps)  # indirect skipped


def test_parse_pom_dependencies_skips_test_scope():
    pom = (
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencies>'
        "<dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId>"
        "<version>33.0.0-jre</version></dependency>"
        "<dependency><groupId>junit</groupId><artifactId>junit</artifactId>"
        "<version>4.13</version><scope>test</scope></dependency>"
        "</dependencies></project>"
    )
    deps = parse_pom_dependencies(pom)
    assert deps == [("com.google.guava:guava", "33.0.0-jre")]


def test_parse_gemfile_dependencies_skips_dev_test_group():
    gemfile = (
        "source 'https://rubygems.org'\n"
        "gem 'rails', '~> 7.1'\n"
        "gem 'pg'\n"
        "group :development, :test do\n"
        "  gem 'rspec'\n"
        "end\n"
        "gem 'puma', '>= 5.0'\n"
    )
    deps = parse_gemfile_dependencies(gemfile)
    assert ("rails", "~> 7.1") in deps
    assert ("pg", None) in deps
    assert ("puma", ">= 5.0") in deps
    assert all(n != "rspec" for n, _ in deps)


def test_parse_csproj_dependencies_attr_and_child():
    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
        '<PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
        '<PackageReference Include="Serilog"><Version>3.1.1</Version></PackageReference>'
        "</ItemGroup></Project>"
    )
    deps = parse_csproj_dependencies(csproj)
    assert ("Newtonsoft.Json", "13.0.3") in deps
    assert ("Serilog", "3.1.1") in deps


def test_parse_mix_dependencies_skips_dev_test():
    mix = (
        "defp deps do [\n"
        '  {:phoenix, "~> 1.7.0"},\n'
        '  {:ecto_sql, "~> 3.10"},\n'
        '  {:credo, "~> 1.7", only: [:dev, :test], runtime: false}\n'
        "] end\n"
    )
    deps = parse_mix_dependencies(mix)
    assert ("phoenix", "~> 1.7.0") in deps
    assert ("ecto_sql", "~> 3.10") in deps
    assert all(n != "credo" for n, _ in deps)


def test_parse_published_names():
    assert parse_gemspec('spec.name = "rails"\n') == "rails"
    assert parse_mix_exs("[app: :my_app, version: \"1.0\"]") == "my_app"


def test_collect_dependencies_across_new_ecosystems():
    texts = {
        "go.mod": "module x\nrequire github.com/a/b v1.0.0\n",
        "Gemfile": "gem 'rails'\n",
        "MyLib.csproj": '<Project><ItemGroup><PackageReference Include="X" Version="1" /></ItemGroup></Project>',
    }
    deps = collect_dependencies(texts)
    ecos = {d.ecosystem for d in deps}
    assert ecos == {"go", "rubygems", "nuget"}
    assert any(d.name == "github.com/a/b" and d.manifest == "go.mod" for d in deps)


# --- extended ecosystems: registry mapping -----------------------------------


def test_map_rubygems():
    gem = {"version": "8.1.0", "downloads": 760_000_000, "licenses": ["MIT"],
           "source_code_uri": "https://github.com/rails/rails"}
    versions = [{"number": "8.1.0", "created_at": "2025-01-01T00:00:00Z"},
                {"number": "8.0.0", "created_at": "2024-01-01T00:00:00Z"}]
    pkg = map_rubygems("rails", gem, versions, "rails/rails")
    assert pkg.ecosystem == "rubygems"
    assert pkg.latest_version == "8.1.0"
    assert pkg.total_downloads == 760_000_000
    assert pkg.monthly_downloads is None  # rubygems exposes no monthly figure
    assert pkg.versions_count == 2
    assert pkg.license == "MIT"
    assert pkg.matches_repo is True


def test_map_hex_recent_and_retirement():
    payload = {
        "releases": [
            {"version": "1.8.0", "inserted_at": "2025-06-01T00:00:00Z"},
            {"version": "1.7.0", "inserted_at": "2024-06-01T00:00:00Z"},
        ],
        "downloads": {"all": 150_000_000, "recent": 3_000_000},
        "meta": {"licenses": ["MIT"], "links": {"GitHub": "https://github.com/phoenixframework/phoenix"}},
        "retirements": {"1.8.0": {"message": "bad"}},
    }
    pkg = map_hex("phoenix", payload, "phoenixframework/phoenix")
    assert pkg.latest_version == "1.8.0"
    assert pkg.monthly_downloads == 1_000_000  # recent / 3
    assert pkg.total_downloads == 150_000_000
    assert pkg.is_deprecated is True  # latest release retired
    assert pkg.matches_repo is True
