"""Full resolved dependency set from GitHub's dependency-graph SBOM export.

The declared **direct** dependency list is parsed from manifest text in
``ecosystems.py``. This module collects **all** dependencies — direct plus
indirect/transitive — from GitHub's precomputed dependency graph
(``/repos/{owner}/{repo}/dependency-graph/sbom``, an SPDX document), one API
call per scan. GitHub builds the graph from the repo's manifests *and*
lockfiles, so it includes the transitive closure whenever a lockfile is
committed.

Same split as the ecosystem adapters:

- **parse_purl / parse_github_sbom / resolve_dependencies** — pure, unit-tested
  without the network.
- **collect_all_dependencies** — network. Strictly best-effort and time-boxed:
  any failure (graph disabled, rate limit, timeout, malformed payload) records
  ``AllDependencies.error`` plus a report warning and the scan continues.

Direct vs. indirect is derived by matching each resolved package against the
already-parsed declared direct set (normalized names). A resolved package that
matches no declared runtime dependency is reported as indirect — note this
also covers direct *dev/test* dependencies, which the declared list
intentionally excludes.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional
from urllib.parse import unquote

from .github import GitHubClient, GitHubError, RepoNotFoundError
from .models import AllDependencies, Dependency, ResolvedDependency

# Hard wall-clock budget for the whole all-dependencies collection step. The
# scan never spends longer than this on the resolved graph; on expiry the
# section reports an error instead.
TIME_BUDGET_SECONDS = 300.0

# Reports stay bounded for giant graphs (a large JS app can resolve to tens of
# thousands of packages): counts are always complete, but at most this many
# packages are embedded in the report, direct entries first.
MAX_PACKAGES_IN_REPORT = 3000

# purl type (https://github.com/package-url/purl-spec) -> our ecosystem label.
PURL_TYPE_ECOSYSTEMS: dict[str, str] = {
    "npm": "npm",
    "pypi": "pypi",
    "cargo": "crates",
    "composer": "packagist",
    "gem": "rubygems",
    "golang": "go",
    "maven": "maven",
    "nuget": "nuget",
    "hex": "hex",
}

# Older SBOM exports name packages "npm:express"-style instead of (or in
# addition to) a purl; map those prefixes to the same ecosystem labels.
NAME_PREFIX_ECOSYSTEMS: dict[str, str] = {
    "npm": "npm",
    "pip": "pypi",
    "pypi": "pypi",
    "cargo": "crates",
    "rust": "crates",
    "composer": "packagist",
    "gem": "rubygems",
    "rubygems": "rubygems",
    "go": "go",
    "golang": "go",
    "maven": "maven",
    "nuget": "nuget",
    "hex": "hex",
}


def parse_purl(purl: str) -> Optional[tuple[str, str, Optional[str]]]:
    """Parse ``pkg:type/namespace/name@version`` into (ecosystem, name, version).

    Returns None for malformed purls and for types outside the ecosystem map
    (notably ``githubactions`` — CI workflow dependencies, not part of the
    shipped software). Percent-encoding is decoded (npm scopes: ``%40scope``).
    """
    if not purl.startswith("pkg:"):
        return None
    body = purl[len("pkg:"):].split("#", 1)[0].split("?", 1)[0]
    parts = [unquote(p) for p in body.split("/") if p]
    if len(parts) < 2:
        return None
    ecosystem = PURL_TYPE_ECOSYSTEMS.get(parts[0].lower())
    if ecosystem is None:
        return None
    version: Optional[str] = None
    last = parts[-1]
    at = last.rfind("@")
    if at > 0:  # `@` at position 0 is an npm scope, not a version separator
        last, version = last[:at], last[at + 1:] or None
    segments = parts[1:-1] + [last]
    # Maven names are group:artifact; every other ecosystem joins with "/"
    # (npm scopes, Go module paths).
    joiner = ":" if ecosystem == "maven" else "/"
    return ecosystem, joiner.join(segments), version


def _package_entry(pkg: dict[str, Any]) -> Optional[tuple[str, str, Optional[str]]]:
    """(ecosystem, name, version) for one SBOM package, or None to skip it."""
    for ref in pkg.get("externalRefs") or []:
        if isinstance(ref, dict) and ref.get("referenceType") == "purl":
            # A purl is authoritative: if its type is unmapped (e.g. GitHub
            # Actions) the package is skipped, not guessed from the name.
            return parse_purl(ref.get("referenceLocator") or "")
    name = pkg.get("name") or ""
    prefix, _, rest = name.partition(":")
    ecosystem = NAME_PREFIX_ECOSYSTEMS.get(prefix.lower()) if rest else None
    if not ecosystem or not rest:
        return None
    return ecosystem, rest, pkg.get("versionInfo") or None


def parse_github_sbom(payload: Any) -> list[tuple[str, str, Optional[str]]]:
    """Extract (ecosystem, name, version) entries from a GitHub SBOM export.

    Skips the repository's own root package(s) (the document's DESCRIBES
    targets), GitHub Actions entries, and unrecognizable packages;
    deduplicates the rest.
    """
    sbom = payload.get("sbom") if isinstance(payload, dict) else None
    if not isinstance(sbom, dict):
        return []
    described = {
        rel.get("relatedSpdxElement")
        for rel in sbom.get("relationships") or []
        if isinstance(rel, dict) and rel.get("relationshipType") == "DESCRIBES"
    }
    entries: list[tuple[str, str, Optional[str]]] = []
    seen: set[tuple[str, str, Optional[str]]] = set()
    for pkg in sbom.get("packages") or []:
        if not isinstance(pkg, dict) or pkg.get("SPDXID") in described:
            continue
        entry = _package_entry(pkg)
        if entry and entry not in seen:
            seen.add(entry)
            entries.append(entry)
    return entries


_PYPI_NAME_SEPARATORS = re.compile(r"[-_.]+")


def _normalized_key(ecosystem: str, name: str) -> tuple[str, str]:
    """Match key for a package name; PyPI names get PEP 503 normalization."""
    if ecosystem == "pypi":
        name = _PYPI_NAME_SEPARATORS.sub("-", name)
    return ecosystem, name.lower()


def resolve_dependencies(
    entries: list[tuple[str, str, Optional[str]]], declared: list[Dependency]
) -> list[ResolvedDependency]:
    """Classify resolved packages as direct (matches a declared runtime
    dependency) or indirect, sorted direct-first for stable truncation."""
    declared_keys = {_normalized_key(d.ecosystem, d.name) for d in declared}
    resolved = [
        ResolvedDependency(
            ecosystem=ecosystem,
            name=name,
            version=version,
            direct=_normalized_key(ecosystem, name) in declared_keys,
        )
        for ecosystem, name, version in entries
    ]
    # Version participates so duplicate names (a package resolved at two
    # versions) order deterministically — GitHub's SBOM export order varies
    # between calls, and report diffs should not.
    resolved.sort(key=lambda dep: (not dep.direct, dep.ecosystem, dep.name.lower(), dep.version or ""))
    return resolved


def collect_all_dependencies(
    gh: GitHubClient,
    owner: str,
    repo: str,
    declared: list[Dependency],
    warnings: list[str],
    budget_seconds: float = TIME_BUDGET_SECONDS,
) -> AllDependencies:
    """Collect the full resolved dependency set, best-effort.

    Never raises: every failure mode fills ``AllDependencies.error`` and adds
    a report warning. The whole step runs under ``budget_seconds`` of
    wall-clock time (enforced as the request timeout).
    """
    result = AllDependencies()
    deadline = time.monotonic() + budget_seconds

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        result.error = "Dependency graph collection skipped: time budget exhausted"
        warnings.append(result.error)
        return result

    payload: Any = None
    try:
        payload = gh.get(
            f"/repos/{owner}/{repo}/dependency-graph/sbom", timeout=remaining
        )
    except RepoNotFoundError:
        result.error = (
            "GitHub dependency-graph SBOM unavailable (404); the dependency "
            "graph may be disabled for this repository"
        )
    except GitHubError as exc:
        result.error = f"GitHub dependency-graph SBOM request failed: {exc}"
    except Exception as exc:  # never let this step break a scan
        result.error = f"Unexpected error collecting the dependency graph: {exc}"

    if result.error is None and not (
        isinstance(payload, dict) and isinstance(payload.get("sbom"), dict)
    ):
        result.error = "GitHub dependency-graph SBOM response was not a valid SPDX document"

    if result.error:
        warnings.append(result.error)
        return result

    packages = resolve_dependencies(parse_github_sbom(payload), declared)
    result.collected = True
    result.source = "github-sbom"
    result.total_count = len(packages)
    result.direct_count = sum(1 for dep in packages if dep.direct)
    result.indirect_count = result.total_count - result.direct_count
    if len(packages) > MAX_PACKAGES_IN_REPORT:
        result.truncated = True
        warnings.append(
            f"Resolved dependency list truncated to {MAX_PACKAGES_IN_REPORT} of "
            f"{len(packages)} packages (counts remain complete)"
        )
        packages = packages[:MAX_PACKAGES_IN_REPORT]
    result.packages = packages
    return result
