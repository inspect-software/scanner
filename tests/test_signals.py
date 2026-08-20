import base64

from scanner.collect import (
    _activity,
    _dependencies,
    _enrich_top_contributors,
    _owner_profile,
    _quality,
    _security,
    _security_contacts,
    _semver_sort_key,
)
from scanner.github import GitHubError
from scanner.models import CommunityHealth, Contributor
from scanner.snapshot import RepoSnapshot


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


class _FakeContents:
    """Stand-in for GitHubClient serving /contents/ and /users/ payloads."""

    def __init__(self, contents=None, user=None):
        self._contents = contents or {}
        self._user = user
        self.paths = []

    def get_optional(self, path, params=None, timeout=None):
        self.paths.append(path)
        if "/contents/" in path:
            body = self._contents.get(path.split("/contents/", 1)[1])
            if body is None:
                return None
            return {"encoding": "base64", "content": base64.b64encode(body.encode()).decode()}
        if path.startswith("/users/"):
            return self._user
        return None


def test_security_contacts_read_the_policy_file():
    gh = _FakeContents({"SECURITY.md": "Mail security@acme.io to report a flaw.\n"})
    channels = _security_contacts(gh, "/repos/acme/tool", TREE, [])
    assert [(c.value, c.role, c.source) for c in channels] == [
        ("security@acme.io", "security", "SECURITY.md")
    ]


def test_security_contacts_skip_the_fetch_when_no_policy_exists():
    gh = _FakeContents()
    warnings = []
    assert _security_contacts(gh, "/repos/acme/tool", ["README.md"], warnings) == []
    # The point of gating on the tree: no policy, no request.
    assert gh.paths == []
    assert warnings == []


def test_security_contacts_warn_but_do_not_fail_when_unreadable():
    gh = _FakeContents()  # tree says SECURITY.md exists; the fetch returns nothing
    warnings = []
    assert _security_contacts(gh, "/repos/acme/tool", TREE, warnings) == []
    assert len(warnings) == 1 and "SECURITY.md" in warnings[0]


def test_security_contacts_use_the_path_as_spelled_in_the_tree():
    gh = _FakeContents({".github/SECURITY.md": "Contact security@acme.io."})
    channels = _security_contacts(gh, "/repos/acme/tool", [".github/SECURITY.md"], [])
    assert channels[0].source == ".github/SECURITY.md"


def test_owner_profile_diverts_contacts_off_the_published_profile():
    gh = _FakeContents(user={
        "login": "acme",
        "type": "Organization",
        "name": "Acme",
        "blog": "https://acme.io",
        "email": "team@acme.io",
        "twitter_username": "acme",
        "location": "Berlin, Germany",
        "followers": 10,
    })
    contacts = []
    profile = _owner_profile(gh, {"owner": {"login": "acme"}}, [], contacts)

    # The contact data is captured...
    assert ("email", "team@acme.io") in [(c.kind, c.value) for c in contacts]
    assert ("handle", "@acme") in [(c.kind, c.value) for c in contacts]
    # ...and stays off the model that gets published.
    assert not hasattr(profile, "email")
    assert not hasattr(profile, "twitter_username")
    assert profile.blog == "https://acme.io"
    assert profile.location == "Berlin, Germany"


def test_owner_profile_without_a_contacts_list_still_works():
    gh = _FakeContents(user={"login": "acme", "type": "User", "email": "a@acme.io"})
    assert _owner_profile(gh, {"owner": {"login": "acme"}}, []).login == "acme"


class _FakeContributorProfiles:
    def __init__(self, result=None, error=None, token="token"):
        self.token = token
        self.result = result or {}
        self.error = error
        self.calls = []

    def graphql(self, query, variables):
        self.calls.append((query, variables))
        if self.error:
            raise self.error
        return self.result


def test_top_contributor_profiles_are_batched_in_one_graphql_request():
    gh = _FakeContributorProfiles(
        {
            "contributor0": {
                "name": "Alice Example",
                "location": "Berlin",
                "company": "@acme",
                "organizations": {
                    "nodes": [{"login": "acme", "name": "Acme Inc.", "location": "Paris"}]
                },
            }
        }
    )
    contributors = [
        Contributor(login="alice", commits=100, type="User", avatar_url="https://example/alice"),
        Contributor(login="dependabot[bot]", commits=20, type="Bot"),
    ]
    warnings = []

    _enrich_top_contributors(gh, contributors, warnings)

    assert len(gh.calls) == 1
    assert gh.calls[0][1] == {"login0": "alice"}
    assert contributors[0].profile.location == "Berlin"
    assert contributors[0].profile.organizations[0].login == "acme"
    assert contributors[0].profile.organizations[0].location == "Paris"
    assert contributors[1].profile is None
    assert warnings == []


def test_top_contributor_profile_failure_preserves_original_data():
    gh = _FakeContributorProfiles(error=GitHubError("rate limited"))
    contributor = Contributor(login="alice", commits=100, type="User")
    warnings = []

    _enrich_top_contributors(gh, [contributor], warnings)

    assert contributor.profile is None
    assert warnings == ["Contributor profile enrichment unavailable: rate limited"]


def test_top_contributor_profiles_cost_no_request_without_token():
    gh = _FakeContributorProfiles(token=None)
    contributor = Contributor(login="alice", commits=100, type="User")

    _enrich_top_contributors(gh, [contributor], [])

    assert gh.calls == []


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


def test_activity_falls_back_to_semver_tags_via_rest():
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
    snapshot = RepoSnapshot(repo={"pushed_at": "2026-06-10T00:00:00Z"})  # tags=None: REST path
    activity = _activity(gh, "/repos/o/r", snapshot, [])

    assert activity.releases_from_tags is True
    assert activity.releases_count == 2  # only semver tags
    assert activity.latest_release_tag == "v1.2.0"
    assert activity.days_since_latest_release is not None
    assert activity.mean_days_between_releases == 31.0


def test_activity_falls_back_to_semver_tags_from_snapshot():
    """GraphQL snapshots carry tag dates inline: no /tags or /commits calls."""
    gh = _FakeGitHub([], {})  # would return no tags: proves they aren't fetched
    snapshot = RepoSnapshot(
        repo={"pushed_at": "2026-06-10T00:00:00Z"},
        tags=[
            {"name": "v1.2.0", "date": "2026-06-01T00:00:00Z"},
            {"name": "v1.1.0", "date": "2026-05-01T00:00:00Z"},
            {"name": "nightly", "date": "2026-06-05T00:00:00Z"},  # not semver
        ],
    )
    activity = _activity(gh, "/repos/o/r", snapshot, [])

    assert activity.releases_from_tags is True
    assert activity.releases_count == 2
    assert activity.latest_release_tag == "v1.2.0"
    assert activity.mean_days_between_releases == 31.0


def test_activity_no_releases_and_no_tags():
    gh = _FakeGitHub([], {})
    activity = _activity(gh, "/repos/o/r", RepoSnapshot(repo={}), [])
    assert activity.releases_count == 0
    assert activity.releases_from_tags is False
