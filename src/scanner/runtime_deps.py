"""Runtime dependency closure of the *published* package, from deps.dev.

The repository dependency graph that ``sbom.py`` collects answers "what does
this repository install to build and test itself". For a repository that
publishes a package, that is the wrong question for a consumer: it mixes in
development and test pins that no installer ever downloads. Measuring
``pallets/flask`` against its repository graph assesses 106 packages, most of
them Sphinx, pytest and tox; measuring the published ``flask`` assesses 7.

deps.dev (Google's open dependency index) resolves the runtime closure of one
published version in a single unauthenticated call, so the advisory metric can
ask the question that matters — *what does installing this package actually
pull in* — and fall back to the repository graph only when the repository
publishes nothing.

Same split as the rest of the scanner: ``depsdev_system`` /
``parse_dependency_nodes`` are pure; ``collect_runtime_closure`` is network and
strictly best-effort — any failure returns None and the caller falls back.
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .models import EcosystemPackage, ResolvedDependency

DEPSDEV_URL = (
    "https://api.deps.dev/v3alpha/systems/{system}/packages/{name}/versions/{version}"
    ":dependencies"
)

TIME_BUDGET_SECONDS = 30.0

# Our ecosystem labels -> deps.dev system names.
#
# Only these four resolve a dependency *graph*. deps.dev indexes more
# ecosystems for other endpoints, but `:dependencies` returns 404 for Go,
# NuGet, RubyGems, Packagist and Hex (verified 2026-07-20 against known-good
# packages in each). Listing them here would spend a request and emit a
# per-repository warning for a limitation that is not the repository's;
# instead those ecosystems fall back to the repository graph silently, and the
# report's scope field says which set was assessed.
DEPSDEV_SYSTEMS: dict[str, str] = {
    "npm": "npm",
    "pypi": "pypi",
    "crates": "cargo",
    "maven": "maven",
}


def depsdev_system(ecosystem: str) -> Optional[str]:
    """deps.dev's system name for one of our ecosystem keys, or None."""
    return DEPSDEV_SYSTEMS.get(ecosystem.lower())


def parse_dependency_nodes(payload: Any, ecosystem: str) -> list[ResolvedDependency]:
    """Resolved runtime dependencies from a deps.dev `:dependencies` payload.

    The graph's first node is the queried package itself (``relation: SELF``)
    and is skipped. ``DIRECT`` maps to our ``direct`` flag; everything deeper
    is transitive.
    """
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, list):
        return []
    resolved: list[ResolvedDependency] = []
    seen: set[tuple[str, str]] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        relation = node.get("relation")
        if relation == "SELF":
            continue
        key = node.get("versionKey") or {}
        name, version = key.get("name"), key.get("version")
        if not name:
            continue
        dedupe = (name.lower(), version or "")
        if dedupe in seen:
            continue
        seen.add(dedupe)
        resolved.append(
            ResolvedDependency(
                ecosystem=ecosystem,
                name=name,
                version=version or None,
                direct=relation == "DIRECT",
            )
        )
    resolved.sort(key=lambda d: (not d.direct, d.name.lower(), d.version or ""))
    return resolved


def _name_key(value: str) -> str:
    """Comparison key for matching a package name to a repository name."""
    base = value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return "".join(c for c in base.lower() if c.isalnum())


def primary_package(
    packages: list[EcosystemPackage], repo_name: str = ""
) -> Optional[EcosystemPackage]:
    """The published package to assess.

    A monorepo publishes many packages and assessing all of them would multiply
    the call count for diminishing value, so exactly one is chosen and the
    report names it.

    **The one that shares the repository's name wins.** Taking the first
    resolvable entry instead picked ``examples`` for tokio-rs/tokio and
    ``actioncable`` for rails/rails — assessing an incidental sibling crate or
    gem while appearing to assess the project everybody actually installs.
    """
    candidates = [p for p in packages if p.latest_version and depsdev_system(p.ecosystem)]
    if not candidates:
        return None
    if repo_name:
        wanted = _name_key(repo_name)
        for pkg in candidates:
            if _name_key(pkg.name) == wanted:
                return pkg
    return candidates[0]


def collect_runtime_closure(
    package: EcosystemPackage,
    warnings: list[str],
    *,
    budget_seconds: float = TIME_BUDGET_SECONDS,
    client: Optional[httpx.Client] = None,
) -> Optional[list[ResolvedDependency]]:
    """Runtime closure of one published package version; None on any failure.

    Returning None is not an error state — the caller falls back to the
    repository dependency graph and says so in the report.
    """
    system = depsdev_system(package.ecosystem)
    if not system or not package.latest_version:
        return None

    url = DEPSDEV_URL.format(
        system=system,
        name=quote(package.name, safe=""),
        version=quote(package.latest_version, safe=""),
    )
    owns_client = client is None
    http = client or httpx.Client()
    deadline = time.monotonic() + budget_seconds
    try:
        response = http.get(url, timeout=max(1.0, deadline - time.monotonic()))
        if response.status_code == 404:
            warnings.append(
                f"deps.dev does not index {package.ecosystem}:{package.name}@"
                f"{package.latest_version}; advisories assessed against the "
                "repository dependency graph instead"
            )
            return None
        response.raise_for_status()
        resolved = parse_dependency_nodes(response.json(), package.ecosystem)
        return resolved or None
    except Exception as exc:  # never let this step break a scan
        warnings.append(
            f"Runtime dependency closure could not be resolved from deps.dev ({exc}); "
            "advisories assessed against the repository dependency graph instead"
        )
        return None
    finally:
        if owns_client:
            http.close()
