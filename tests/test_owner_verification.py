"""The verified-domain flag is read from /orgs/, and unknown is not false.

GitHub serves organizations from both ``/users/{login}`` and
``/orgs/{login}``, but only the second carries ``is_verified`` — the first
omits the key rather than returning false. Reading it from the /users/
payload silently scored every organization in the record as unverified;
measured on scylladb, vuejs, pallets and facebook, all of which report
``is_verified: true`` from /orgs/ and nothing at all from /users/, and
reported by a maintainer reading their own report
(scylladb/scylla-rust-driver#1852).
"""

from __future__ import annotations

import pytest

from scanner.collect import _org_verified_domain, _owner_profile
from scanner.metrics import metric_stewardship
from scanner.models import OwnerProfile, RepoData


class _FakeAccounts:
    """Serves /users/{login} and /orgs/{login} the way GitHub actually does."""

    def __init__(self, user: dict | None, org: dict | None = None):
        self._user = user
        self._org = org
        self.paths: list[str] = []

    def get_optional(self, path, params=None, timeout=None):
        self.paths.append(path)
        if path.startswith("/users/"):
            return self._user
        if path.startswith("/orgs/"):
            return self._org
        return None


ORG_USER_PAYLOAD = {
    # Exactly what /users/{org} returns: no is_verified key at all.
    "login": "scylladb",
    "type": "Organization",
    "followers": 900,
    "public_repos": 120,
    "created_at": "2015-01-01T00:00:00Z",
}


def _repo(login="scylladb", type_="Organization"):
    return {"owner": {"login": login, "type": type_}}


# --- collection ------------------------------------------------------------


def test_verified_domain_comes_from_the_orgs_endpoint():
    gh = _FakeAccounts(ORG_USER_PAYLOAD, {"login": "scylladb", "is_verified": True})
    profile = _owner_profile(gh, _repo(), [])

    assert profile.is_verified is True
    assert "/orgs/scylladb" in gh.paths


def test_unverified_organization_reads_as_false_not_unknown():
    gh = _FakeAccounts(ORG_USER_PAYLOAD, {"login": "acme", "is_verified": False})
    assert _owner_profile(gh, _repo(), []).is_verified is False


def test_failed_org_lookup_leaves_the_flag_unknown():
    """A request that did not happen is not evidence of an unverified domain."""
    gh = _FakeAccounts(ORG_USER_PAYLOAD, None)
    assert _owner_profile(gh, _repo(), []).is_verified is None


def test_personal_accounts_never_pay_the_extra_request():
    user = {"login": "torvalds", "type": "User", "followers": 200_000, "public_repos": 8}
    gh = _FakeAccounts(user)
    profile = _owner_profile(gh, _repo("torvalds", "User"), [])

    assert profile.is_verified is None
    assert not any(p.startswith("/orgs/") for p in gh.paths)


def test_org_endpoint_without_the_key_is_unknown():
    gh = _FakeAccounts(ORG_USER_PAYLOAD, {"login": "acme"})
    assert _org_verified_domain(gh, "acme") is None


# --- scoring ---------------------------------------------------------------


def _stewardship(**owner_kw):
    owner = OwnerProfile(login="acme", type="Organization", followers=100,
                         public_repos=10, account_age_days=2000, **owner_kw)
    return metric_stewardship(RepoData(owner=owner))


def _component(metric, name):
    return next(c for c in metric.components if c.name == name)


def test_verified_organization_scores_the_component():
    assert _component(_stewardship(is_verified=True), "Verified domain").status == "met"


def test_unverified_organization_misses_it():
    assert _component(_stewardship(is_verified=False), "Verified domain").status == "missed"


def test_unknown_verification_is_excluded_not_missed():
    """Every report written before scanner 0.34.0 carries None here. Scoring
    that as 'unverified' marked the whole organization-owned record down for a
    request that was never made."""
    assert _component(_stewardship(is_verified=None), "Verified domain").status == "excluded"


def test_unknown_scores_higher_than_confirmed_unverified():
    unknown = _stewardship(is_verified=None)
    unverified = _stewardship(is_verified=False)
    assert unknown.value > unverified.value


def test_unknown_scores_no_higher_than_confirmed_verified():
    """Renormalizing must not turn ignorance into an advantage."""
    assert _stewardship(is_verified=None).value <= _stewardship(is_verified=True).value
