from scanner.ecosystems import (
    identify_packages,
    manifest_paths,
    map_crates,
    map_npm,
    map_packagist,
    map_pypi,
    parse_cargo_toml,
    parse_composer_json,
    parse_package_json,
    parse_pyproject,
    parse_setup_cfg,
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
