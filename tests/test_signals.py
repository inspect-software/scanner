from scanner.collect import _dependencies, _quality, _security
from scanner.models import CommunityHealth


TREE = [
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    ".editorconfig",
    ".pre-commit-config.yaml",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql-analysis.yml",
    ".github/dependabot.yml",
    "src/app/main.py",
    "tests/test_main.py",
    "docs/index.md",
]


def test_quality_signals():
    q = _quality(TREE)
    assert q.has_ci
    assert sorted(q.ci_workflows) == ["ci.yml", "codeql-analysis.yml"]
    assert q.has_tests
    assert q.has_docs_dir
    assert q.has_editorconfig
    assert q.has_precommit_config
    assert q.has_linter_config  # via pre-commit


def test_security_signals():
    s = _security(TREE, CommunityHealth())
    assert s.has_security_policy
    assert s.has_dependabot_config
    assert s.has_codeql_workflow
    assert s.lockfiles == ["uv.lock"]


def test_dependency_signals():
    d = _dependencies(TREE)
    assert d.manifests == ["package.json", "pyproject.toml"]
    assert d.ecosystems == ["npm", "pypi"]


def test_empty_tree():
    q = _quality([])
    assert not q.has_ci and not q.has_tests
    assert _dependencies([]).ecosystems == []
