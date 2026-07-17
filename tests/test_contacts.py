from scanner.contacts import (
    dedupe,
    from_crates,
    from_hex,
    from_npm,
    from_owner_profile,
    from_packagist,
    from_pypi,
    from_rubygems,
    from_security_policy,
)


def _values(channels, role=None):
    return [c.value for c in channels if role is None or c.role == role]


# --- address hygiene ----------------------------------------------------------


def test_unroutable_addresses_are_dropped():
    payload = {"info": {"author_email": "dev@users.noreply.github.com"}}
    assert from_pypi(payload) == []


def test_placeholder_domains_are_dropped():
    payload = {"info": {"author_email": "you@example.com", "maintainer_email": "a@test.com"}}
    assert from_pypi(payload) == []


def test_noreply_localpart_is_dropped():
    assert from_pypi({"info": {"author_email": "no-reply@djangoproject.com"}}) == []


def test_angle_bracket_form_yields_only_the_address():
    payload = {"author": "Jane Doe <jane@example.org>"}
    assert from_npm(payload) == []  # example.org is a placeholder domain

    payload = {"author": "Jane Doe <jane@djangoproject.com>"}
    assert _values(from_npm(payload)) == ["jane@djangoproject.com"]


def test_version_strings_are_not_mistaken_for_addresses():
    assert from_security_policy("Use flask@2.0 or later.", "SECURITY.md") == []


def test_dedupe_keeps_first_and_is_case_insensitive():
    channels = from_pypi(
        {"info": {"author_email": "Jane@Acme.io", "maintainer_email": "jane@acme.io"}}
    )
    # Same address, different roles: both are kept, since the role is the fact.
    assert sorted(c.role for c in channels) == ["author", "maintainer"]

    same_role = dedupe(list(channels) + list(channels))
    assert len(same_role) == 2


# --- GitHub -------------------------------------------------------------------


def test_owner_profile_email_and_handle():
    channels = from_owner_profile(
        {"email": "team@acme.io", "twitter_username": "acme", "blog": "https://acme.io"}
    )
    assert [(c.kind, c.value, c.role) for c in channels] == [
        ("email", "team@acme.io", "owner"),
        ("handle", "@acme", "owner"),
        ("url", "https://acme.io", "owner"),
    ]
    assert all(c.source == "github-owner-profile" for c in channels)


def test_owner_profile_without_published_contacts():
    assert from_owner_profile({"login": "acme", "followers": 3}) == []


def test_owner_profile_handle_is_normalized_once():
    channels = from_owner_profile({"twitter_username": "@acme"})
    assert _values(channels) == ["@acme"]


def test_owner_profile_ignores_non_http_blog():
    assert from_owner_profile({"blog": "acme.io"}) == []


def test_security_policy_extracts_address_and_reporting_link():
    text = (
        "# Security Policy\n\n"
        "Report vulnerabilities to security@acme.io.\n"
        "Or [open an advisory](https://github.com/acme/tool/security/advisories/new).\n"
        "See our [homepage](https://acme.io) for other things.\n"
    )
    channels = from_security_policy(text, ".github/SECURITY.md")
    assert _values(channels, "security") == [
        "security@acme.io",
        "https://github.com/acme/tool/security/advisories/new",
    ]
    assert all(c.source == ".github/SECURITY.md" for c in channels)


def test_security_policy_without_contacts():
    assert from_security_policy("We support the latest release only.", "SECURITY.md") == []


# --- registries ---------------------------------------------------------------


def test_pypi_author_maintainer_and_project_urls():
    payload = {
        "info": {
            "author_email": "author@acme.io",
            "maintainer_email": "maint@acme.io",
            "project_urls": {
                "Funding": "https://github.com/sponsors/acme",
                "Issue Tracker": "https://github.com/acme/tool/issues",
                "Chat": "https://discord.gg/acme",
                "Documentation": "https://docs.acme.io",
            },
        }
    }
    channels = from_pypi(payload)
    assert _values(channels, "author") == ["author@acme.io"]
    assert _values(channels, "maintainer") == ["maint@acme.io"]
    assert _values(channels, "funding") == ["https://github.com/sponsors/acme"]
    assert _values(channels, "issues") == ["https://github.com/acme/tool/issues"]
    assert _values(channels, "chat") == ["https://discord.gg/acme"]
    # Documentation is not a contact channel.
    assert "https://docs.acme.io" not in _values(channels)


def test_npm_maintainers_bugs_and_funding():
    payload = {
        "author": {"name": "Jane", "email": "jane@acme.io"},
        "maintainers": [{"name": "joe", "email": "joe@acme.io"}, {"name": "bot"}],
        "bugs": {"url": "https://github.com/acme/tool/issues"},
        "funding": [{"type": "opencollective", "url": "https://opencollective.com/acme"}],
    }
    channels = from_npm(payload)
    assert _values(channels, "author") == ["jane@acme.io"]
    assert _values(channels, "maintainer") == ["joe@acme.io"]
    assert _values(channels, "issues") == ["https://github.com/acme/tool/issues"]
    assert _values(channels, "funding") == ["https://opencollective.com/acme"]


def test_npm_funding_as_bare_string():
    channels = from_npm({"funding": "https://github.com/sponsors/acme"})
    assert _values(channels, "funding") == ["https://github.com/sponsors/acme"]


def test_npm_bugs_as_bare_url_string():
    channels = from_npm({"bugs": "https://github.com/acme/tool/issues"})
    assert _values(channels, "issues") == ["https://github.com/acme/tool/issues"]


def test_npm_empty_payload():
    assert from_npm({}) == []


def test_rubygems_links_and_authors():
    payload = {
        "authors": "Jane Doe <jane@acme.io>, John Roe",
        "bug_tracker_uri": "https://github.com/acme/gem/issues",
        "mailing_list_uri": "https://groups.google.com/g/acme",
    }
    channels = from_rubygems(payload)
    assert _values(channels, "author") == ["jane@acme.io"]
    assert _values(channels, "issues") == ["https://github.com/acme/gem/issues"]
    assert _values(channels, "support") == ["https://groups.google.com/g/acme"]


def test_rubygems_authors_without_address():
    assert from_rubygems({"authors": "Jane Doe, John Roe"}) == []


def test_hex_links():
    payload = {"meta": {"links": {"GitHub": "https://github.com/acme/tool",
                                  "Discord": "https://discord.gg/acme"}}}
    channels = from_hex(payload)
    assert _values(channels, "chat") == ["https://discord.gg/acme"]
    # The repository link is already reported as repository_url; not a contact.
    assert "https://github.com/acme/tool" not in _values(channels)


def test_packagist_maintainer_emails():
    payload = {"package": {"maintainers": [{"name": "jane", "email": "jane@acme.io"},
                                           {"name": "anon"}]}}
    assert _values(from_packagist(payload), "maintainer") == ["jane@acme.io"]


def test_crates_exposes_no_addresses():
    payload = {"crate": {"homepage": "https://acme.io", "documentation": "https://docs.rs/acme"}}
    assert from_crates(payload) == []
