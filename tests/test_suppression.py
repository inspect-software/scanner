"""An objection under Art. 21 must survive the next scan.

The privacy policy promises that objecting removes a contributor's enriched
profile fields *and* keeps them out of future scans. That promise is only true
if the exclusion happens inside the pipeline, before metrics are computed —
otherwise the next rescan re-collects the profile, and a suppressed location
still moves a jurisdiction score even when it is invisible in the report.
"""

from __future__ import annotations

from scanner.jurisdiction import _located_subjects
from scanner.models import (
    ContactChannel,
    Contributor,
    ContributorOrganization,
    ContributorProfile,
    OwnerProfile,
    RepoData,
)
from scanner.suppression import Suppression, apply_suppression


def _data() -> RepoData:
    data = RepoData()
    data.owner = OwnerProfile(
        login="objector",
        type="User",
        name="A Person",
        company="Example",
        location="Tehran, Iran",
    )
    data.maintainership.top_contributors = [
        Contributor(
            login="objector",
            commits=120,
            profile=ContributorProfile(
                name="A Person",
                location="Moscow, Russia",
                company="Example",
                organizations=[ContributorOrganization(login="example-org")],
            ),
        ),
        Contributor(
            login="someone-else",
            commits=40,
            profile=ContributorProfile(name="Other", location="Berlin"),
        ),
    ]
    data.contacts = [
        ContactChannel(kind="email", value="Objector@Example.com", role="maintainer", source="pypi"),
        ContactChannel(kind="email", value="keep@example.com", role="maintainer", source="pypi"),
    ]
    return data


def test_suppressed_login_loses_its_profile_but_keeps_its_commits():
    data = _data()

    removed = apply_suppression(data, Suppression.of(logins=["objector"]))

    # Two subjects carried the login: the owner profile and the contributor.
    assert removed == 2
    objector, other = data.maintainership.top_contributors
    assert objector.profile is None
    # The commit count is the repository's own public history and the input to
    # the bus factor: removing it would misstate who wrote the software.
    assert objector.commits == 120
    assert objector.login == "objector"
    assert other.profile is not None


def test_matching_ignores_case_on_both_sides():
    data = _data()

    removed = apply_suppression(data, Suppression.of(logins=["OBJECTOR"], emails=["objector@example.com"]))

    assert data.maintainership.top_contributors[0].profile is None
    assert [c.value for c in data.contacts] == ["keep@example.com"]
    assert removed == 3


def test_matching_is_exact_not_by_domain():
    """A list that can match a whole company quietly loses data nobody asked
    to lose, so suppression never matches on a prefix or a domain."""
    data = _data()

    apply_suppression(data, Suppression.of(emails=["example.com"], logins=["object"]))

    assert len(data.contacts) == 2
    assert data.maintainership.top_contributors[0].profile is not None


def test_empty_suppression_changes_nothing():
    data = _data()

    assert apply_suppression(data, None) == 0
    assert apply_suppression(data, Suppression.of()) == 0
    assert len(data.contacts) == 2
    assert all(c.profile is not None for c in data.maintainership.top_contributors)


def test_owner_profile_is_suppressed_too():
    """The owner is a separate subject in the jurisdiction metric — a
    suppression that only cleared contributors would leave the objector's
    location still scoring."""
    data = _data()

    apply_suppression(data, Suppression.of(logins=["objector"]))

    assert data.owner is not None
    assert data.owner.login == "objector"
    assert data.owner.location is None
    assert data.owner.name is None
    assert data.owner.company is None


def test_no_suppressed_login_reaches_the_jurisdiction_metric():
    """The end the objection is actually about: after suppression, nothing the
    metric iterates carries a location for that person."""
    data = _data()

    apply_suppression(data, Suppression.of(logins=["objector"]))

    locations = {
        login.lower(): location for _role, login, location in _located_subjects(data)
    }
    assert locations["objector"] is None
    # Everyone else is untouched.
    assert locations["someone-else"] == "Berlin"
