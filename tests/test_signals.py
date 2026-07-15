from scanner.collect import (
    _activity,
    _dependencies,
    _quality,
    _security,
    _semver_sort_key,
)
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


def test_docs_dir_aliases():
    for directory in ("doc", "docs", "documentation", "wiki"):
        assert _quality([f"{directory}/index.md"]).has_docs_dir, directory
    # A file named like a docs dir, or a bare top-level file, is not a docs dir.
    assert not _quality(["documentation.md"]).has_docs_dir
    assert not _quality(["src/app/main.py"]).has_docs_dir


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


def test_semver_sort_key_orders_versions():
    ordered = ["v2.0.0", "v1.10.0", "v1.9.0", "v1.9.0-rc1"]
    shuffled = ["v1.9.0-rc1", "v2.0.0", "v1.9.0", "v1.10.0"]
    shuffled.sort(key=_semver_sort_key, reverse=True)
    assert shuffled == ordered


class _FakeGitHub:
    """Minimal stand-in for GitHubClient covering the endpoints _activity hits."""

    def __init__(self, tags, commit_dates):
        self._tags = tags
        self._commit_dates = commit_dates  # sha -> ISO string

    def get_optional(self, path, params=None, timeout=None):
        if path.endswith("/releases"):
            return []
        if path.endswith("/tags"):
            return self._tags
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            return {"commit": {"committer": {"date": self._commit_dates[sha]}}}
        return None

    def get_stats(self, path):
        return None


def test_activity_falls_back_to_semver_tags():
    tags = [
        {"name": "v1.2.0", "commit": {"sha": "aaa"}},
        {"name": "v1.1.0", "commit": {"sha": "bbb"}},
        {"name": "nightly", "commit": {"sha": "ccc"}},  # ignored: not semver
    ]
    commit_dates = {
        "aaa": "2026-06-01T00:00:00Z",
        "bbb": "2026-05-01T00:00:00Z",
    }
    gh = _FakeGitHub(tags, commit_dates)
    activity = _activity(gh, "/repos/o/r", {"pushed_at": "2026-06-10T00:00:00Z"}, [])

    assert activity.releases_from_tags is True
    assert activity.releases_count == 2  # only semver tags
    assert activity.latest_release_tag == "v1.2.0"
    assert activity.days_since_latest_release is not None
    assert activity.mean_days_between_releases == 31.0


def test_activity_no_releases_and_no_tags():
    gh = _FakeGitHub([], {})
    activity = _activity(gh, "/repos/o/r", {}, [])
    assert activity.releases_count == 0
    assert activity.releases_from_tags is False
