"""Registries that host documentation for everything they publish.

crates.io builds and serves docs.rs/{crate} for every published crate; the
Cargo.toml `documentation` key merely restates it, and plenty of crates skip
it. Scoring the missing key as "no documentation site" measured paperwork
rather than documentation — reported twice by maintainers reading their own
reports (TedDriggs/darling#437, scylladb/scylla-rust-driver#1852), and syn
and serde carried the same finding.

The table is deliberately narrow: pkg.go.dev is not a peer (measured:
github.com/qax-os/excelize/v2 answers 404) and hexdocs.pm only redirects.
Inventing a documentation site that does not exist is the same error in the
other direction.
"""

from __future__ import annotations

from scanner.metrics import _registry_docs_site, metric_documentation
from scanner.models import EcosystemData, EcosystemPackage, RepoData, RepoInfo


def _pkg(**kw) -> EcosystemPackage:
    base = dict(ecosystem="crates", name="scylla", registry_url="https://crates.io/crates/scylla",
                exists=True, matches_repo=True)
    base.update(kw)
    return EcosystemPackage(**base)


def _data(*packages, homepage=None) -> RepoData:
    return RepoData(
        repo=RepoInfo(homepage=homepage),
        ecosystem=EcosystemData(packages=list(packages)),
    )


def test_crate_without_a_declaration_gets_its_docs_rs_page():
    assert _registry_docs_site(_data(_pkg())) == "https://docs.rs/scylla"


def test_declared_documentation_wins_over_the_implied_page():
    """A crate that names its own docs URL is quoted, not overridden."""
    pkg = _pkg(documentation_url="https://rust-driver.docs.scylladb.com/stable/")
    assert _registry_docs_site(_data(pkg)) == "https://rust-driver.docs.scylladb.com/stable/"


def test_declared_homepage_also_wins_over_the_implied_page():
    pkg = _pkg(homepage_url="https://scylladb.com")
    assert _registry_docs_site(_data(pkg)) == "https://scylladb.com"


def test_unpublished_crate_implies_nothing():
    """docs.rs answers 404 for a name that was never published."""
    assert _registry_docs_site(_data(_pkg(exists=False))) is None


def test_foreign_package_implies_nothing():
    """A crate whose registry entry points elsewhere is not this repo's."""
    assert _registry_docs_site(_data(_pkg(matches_repo=False))) is None


def test_ecosystems_outside_the_table_imply_nothing():
    """Go and Hex look like peers and were measured not to be."""
    for ecosystem, name in (("go", "github.com/qax-os/excelize/v2"), ("hex", "phoenix"),
                            ("pypi", "locust"), ("npm", "axios")):
        data = _data(_pkg(ecosystem=ecosystem, name=name))
        assert _registry_docs_site(data) is None, ecosystem


def test_the_documentation_metric_credits_the_implied_page():
    metric = metric_documentation(_data(_pkg()))
    site = next(c for c in metric.components if "site" in c.name.lower())
    assert site.status == "met"


def test_a_repo_homepage_still_takes_precedence():
    metric = metric_documentation(_data(_pkg(), homepage="https://example.org/docs"))
    site = next(c for c in metric.components if "site" in c.name.lower())
    assert site.status == "met"
