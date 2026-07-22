"""Known advisories affecting the resolved dependency set, from OSV.dev.

``sbom.py`` collects the resolved dependency graph — ecosystem, name and
version for the transitive closure. That tuple is exactly what OSV's batch
query takes, so matching declared versions against public advisories costs
one HTTP call per 1000 packages against a free, unauthenticated API and no
GitHub budget at all.

Same split as the rest of the scanner:

- **osv_ecosystem / build_queries / summarize** — pure, unit-tested without
  the network.
- **collect_advisories** — network. Strictly best-effort and time-boxed: any
  failure (OSV down, timeout, malformed payload) records
  ``DependencyAdvisories.error`` plus a report warning and the scan continues
  with the advisory metric excluded rather than scored zero.

Two deliberate limits, both surfaced in the report rather than hidden:

*Versions.* GitHub's SBOM export sometimes omits ``versionInfo``. An entry
without a version cannot be matched against a version range, so it is skipped
and counted in ``unassessed_count`` — coverage is reported, never assumed.

*Reachability.* An advisory here means "the version recorded in the
dependency graph falls in an advisory's affected range". It is not a claim
that the code path is reachable or the project exploitable. The graph also
includes development and test pins, which GitHub's export does not
distinguish from runtime dependencies, so a finding may concern tooling
rather than shipped software.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

import httpx

from .models import (
    AdvisoryFinding,
    DependencyAdvisories,
    MaliciousDependency,
    ResolvedDependency,
)

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/{vuln_id}"

# OSV accepts at most 1000 queries per batch request.
MAX_BATCH = 1000

# Hard wall-clock budget for the whole advisory step, mirroring sbom.py.
TIME_BUDGET_SECONDS = 120.0

# Detail lookups are per-advisory and only needed for severity. They are capped
# so a repository with an unusually large advisory set cannot dominate a scan;
# beyond the cap findings keep their identifiers and report severity "unknown".
MAX_DETAIL_LOOKUPS = 120

MAX_FINDINGS_IN_REPORT = 250

# Parallel advisory-detail requests. Small on purpose: enough to collapse the
# serial round trips, far below anything that would look like abuse of a free
# public API.
DETAIL_CONCURRENCY = 8

# Advisory records are universal, not per-repository, so one process-wide cache
# makes the steady state free. Bounded so a long-lived worker cannot grow it
# without limit; advisory bodies are a few KB each.
MAX_CACHED_ADVISORIES = 20_000
_PROCESS_DETAIL_CACHE: dict[str, dict] = {}

# Our ecosystem labels are not OSV's. Anything absent here cannot be queried.
OSV_ECOSYSTEMS: dict[str, str] = {
    "npm": "npm",
    "pypi": "PyPI",
    "crates": "crates.io",
    "packagist": "Packagist",
    "rubygems": "RubyGems",
    "go": "Go",
    "maven": "Maven",
    "nuget": "NuGet",
    "hex": "Hex",
}

SEVERITY_ORDER = ["critical", "high", "moderate", "low", "unknown"]

# Fallback severity -> penalty units, used only when no CVSS vector is present.
# CVSS is the preferred input: in a 46-advisory sample a vector was present on
# 95% of records while the coarse database label was present on 76%, so the
# label is both less precise and less available.
SEVERITY_PENALTY: dict[str, float] = {
    "critical": 1.0,
    "high": 0.7,
    "moderate": 0.3,
    "low": 0.1,
    "unknown": 0.3,
}

# CVSS base score -> severity label, per the CVSS v3.1/v4.0 qualitative scale.
CVSS_BANDS: list[tuple[float, str]] = [
    (9.0, "critical"),
    (7.0, "high"),
    (4.0, "moderate"),
    (0.1, "low"),
]

# An advisory whose fix has been available longer than this and is still
# unapplied is a maintenance signal, not bad luck: the project had a full
# quarter to pick up a published fix.
STALE_ADVISORY_DAYS = 90


def cvss_base_score(detail: dict[str, Any]) -> Optional[float]:
    """Highest CVSS base score across an advisory's severity entries.

    OSV carries the full vector string rather than the numeric score, so the
    base score is computed from the vector's metrics. v4.0 vectors are scored
    on their v3-comparable base metrics; the qualitative band is what the
    methodology consumes, and the two scales agree at band boundaries.
    """
    best: Optional[float] = None
    for entry in detail.get("severity") or []:
        if not isinstance(entry, dict):
            continue
        score = _score_vector(entry.get("score") or "")
        if score is not None and (best is None or score > best):
            best = score
    return best


# CVSS v3/v4 base-metric weights (AV/AC/PR/UI/S/C/I/A), per the specification.
_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC = {"L": 0.77, "H": 0.44}
_PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_C = {"N": 0.85, "L": 0.68, "H": 0.5}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def _score_vector(vector: str) -> Optional[float]:
    """CVSS base score from a v3.x or v4.0 vector string, or None."""
    if not vector.startswith("CVSS:"):
        return None
    parts = dict(
        p.split(":", 1) for p in vector.split("/")[1:] if ":" in p
    )
    # v4.0 renames the impact metrics; map them onto the v3 base equivalents.
    conf = parts.get("C") or parts.get("VC")
    integ = parts.get("I") or parts.get("VI")
    avail = parts.get("A") or parts.get("VA")
    try:
        scope_changed = parts.get("S", "U") == "C"
        iss = 1 - (1 - _CIA[conf]) * (1 - _CIA[integ]) * (1 - _CIA[avail])
        if scope_changed:
            impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
        else:
            impact = 6.42 * iss
        if impact <= 0:
            return 0.0
        exploitability = (
            8.22
            * _AV[parts["AV"]]
            * _AC[parts["AC"]]
            * (_PR_C if scope_changed else _PR_U)[parts["PR"]]
            * _UI[parts["UI"]]
        )
        # CVSS v3.1 §8.1: a changed scope multiplies the sum by 1.08 — not 1.5,
        # which inflated every scope-changed vector into the 10.0 cap and made
        # unrelated advisories look identically maximal.
        raw = min((1.08 if scope_changed else 1.0) * (impact + exploitability), 10.0)
        # CVSS rounds up to one decimal.
        return math.ceil(raw * 10) / 10
    except (KeyError, TypeError):
        return None


def penalty_units(severity: str, cvss: Optional[float]) -> float:
    """Penalty units one affected package contributes.

    Prefers the CVSS base score normalized to 0..1 — a continuous, externally
    defined scale — and falls back to the coarse label only where no usable
    vector exists.
    """
    if cvss is not None:
        return round(cvss / 10.0, 3)
    return SEVERITY_PENALTY.get(severity, 0.3)


def severity_from_score(score: float) -> str:
    for threshold, label in CVSS_BANDS:
        if score >= threshold:
            return label
    return "low"


def osv_ecosystem(ecosystem: str) -> Optional[str]:
    """OSV's label for one of our ecosystem keys, or None if unsupported."""
    return OSV_ECOSYSTEMS.get(ecosystem.lower())


# Leading characters that mark a version *constraint* rather than a resolved
# version. Some SBOM entries carry the manifest range ("^4.0.0") instead of the
# version actually locked; querying those asks OSV a question about a string it
# will not interpret the way the range means, so they are skipped and counted.
_CONSTRAINT_PREFIXES = ("^", "~", ">", "<", "=", "!", "*", "v^", "v~")


def is_concrete_version(version: Optional[str]) -> bool:
    """True when a version string names one release rather than a range."""
    if not version:
        return False
    value = version.strip()
    if not value or value.startswith(_CONSTRAINT_PREFIXES):
        return False
    # Ranges also arrive as "1.2 - 1.9", ">=1,<2" or "1.x || 2.x".
    return not any(token in value for token in (" ", ",", "||", "x.", ".x", "*"))


def build_queries(
    packages: Iterable[ResolvedDependency],
) -> tuple[list[ResolvedDependency], list[dict[str, Any]], int]:
    """Split resolved packages into queryable and skipped.

    Returns (queryable packages, OSV query payloads, skipped count). A package
    is skipped when its ecosystem has no OSV equivalent, or its version is
    missing or is a constraint rather than a resolved release.
    """
    queryable: list[ResolvedDependency] = []
    queries: list[dict[str, Any]] = []
    skipped = 0
    for pkg in packages:
        eco = osv_ecosystem(pkg.ecosystem)
        if not eco or not is_concrete_version(pkg.version):
            skipped += 1
            continue
        queryable.append(pkg)
        queries.append({"package": {"name": pkg.name, "ecosystem": eco}, "version": pkg.version})
    return queryable, queries, skipped


def is_malicious_id(vuln_id: str) -> bool:
    """Whether an OSV identifier names a malicious-package report.

    OSV assigns the ``MAL-`` prefix to everything it imports from the OpenSSF
    ``ossf/malicious-packages`` corpus, and it deduplicates GitHub's malware
    advisories into that record, so the identifier alone settles the question
    for the batch response.

    That matters more than elegance here: advisory *details* are capped at
    ``MAX_DETAIL_LOOKUPS`` per scan, so a classification that needed the record
    body would silently miss malware on a repository with a large advisory set.
    """
    return vuln_id.startswith("MAL-")


def is_malicious_record(detail: dict[str, Any]) -> bool:
    """Whether a fetched OSV record is a malicious-package report.

    The identifier is authoritative; the record body is a fallback for the
    routes that do not produce a ``MAL-`` id — GitHub's malware advisories,
    which arrive under a ``GHSA-`` id and carry either OSV's provenance block
    or the corpus's fixed summary wording.
    """
    if is_malicious_id(str(detail.get("id") or "")):
        return True
    if (detail.get("database_specific") or {}).get("malicious-packages-origins"):
        return True
    if str(detail.get("summary") or "").startswith("Malicious code in"):
        return True
    return any(is_malicious_id(str(alias)) for alias in detail.get("aliases") or [])


def severity_of(detail: dict[str, Any]) -> str:
    """Normalized severity for one OSV advisory record.

    Prefers the database's own label (GHSA records carry CRITICAL/HIGH/
    MODERATE/LOW); returns "unknown" when absent, which is common for PYSEC
    records and is reported as such rather than guessed.
    """
    raw = (detail.get("database_specific") or {}).get("severity")
    if isinstance(raw, str) and raw.lower() in SEVERITY_ORDER:
        return raw.lower()
    return "unknown"


def fixed_version_of(detail: dict[str, Any]) -> Optional[str]:
    """Highest ``fixed`` event across the advisory's affected ranges.

    Git commit hashes appear as fixed events for some ecosystems; they are not
    actionable as an upgrade target, so only values that look like versions are
    returned.
    """
    candidates: list[str] = []
    for affected in detail.get("affected") or []:
        for rng in (affected or {}).get("ranges") or []:
            for event in (rng or {}).get("events") or []:
                fixed = (event or {}).get("fixed")
                if isinstance(fixed, str) and fixed and not _looks_like_commit(fixed):
                    candidates.append(fixed)
    if not candidates:
        return None
    return sorted(candidates, key=_version_key)[-1]


def _published_at(detail: dict[str, Any]) -> Optional[datetime]:
    """When OSV published the record, as an aware UTC datetime."""
    raw = detail.get("published")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published


def _published_days_ago(detail: dict[str, Any], now: datetime) -> Optional[int]:
    """Days since the advisory was published — a proxy for how long a fix has
    been available. OSV records carry `published` universally (46/46 in
    sampling), so this is a reliable dimension, unlike the coarse label."""
    published = _published_at(detail)
    if published is None:
        return None
    return max(0, (now - published).days)


def _looks_like_commit(value: str) -> bool:
    return len(value) >= 32 and all(c in "0123456789abcdef" for c in value.lower())


def _version_key(value: str) -> tuple:
    """Rough version ordering; numeric segments compare numerically."""
    parts: list[tuple[int, Any]] = []
    for chunk in value.replace("-", ".").split("."):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, chunk))
    return tuple(parts)


def summarize(
    packages: list[ResolvedDependency],
    results: list[dict[str, Any]],
    details: dict[str, dict[str, Any]],
    skipped: int,
    scope: str = "repository_graph",
    assessed_package: Optional[str] = None,
    now: Optional[datetime] = None,
) -> DependencyAdvisories:
    """Build the advisory summary from a batch response and advisory details.

    ``results`` is OSV's per-query result list, positionally aligned with
    ``packages``. ``details`` maps advisory id -> record; ids missing from it
    contribute severity "unknown".

    Malicious-package reports are split out rather than scored as advisories.
    A package OSV reports as malware is a categorically different finding from
    a package with a vulnerability: there is no fix version, no severity
    vector, and no partial exposure. Left in the advisory list it would score
    as "unknown" severity — 0.3 penalty units, the weight of a moderate CVE.
    A package with both kinds of record moves wholly into the malicious list;
    its CVEs are moot once the package itself is malware.
    """
    now = now or datetime.now(timezone.utc)
    findings: list[AdvisoryFinding] = []
    malicious: list[MaliciousDependency] = []
    for pkg, result in zip(packages, results):
        vulns = (result or {}).get("vulns") or []
        ids = sorted({v.get("id") for v in vulns if isinstance(v, dict) and v.get("id")})
        if not ids:
            continue
        bad = [i for i in ids if is_malicious_id(i) or is_malicious_record(details.get(i, {}))]
        if bad:
            reported = [d for d in (details.get(i) for i in bad) if d]
            published = [p for p in (_published_at(d) for d in reported) if p is not None]
            malicious.append(
                MaliciousDependency(
                    ecosystem=pkg.ecosystem,
                    name=pkg.name,
                    version=pkg.version,
                    direct=pkg.direct,
                    advisory_ids=bad[:10],
                    first_reported_at=min(published) if published else None,
                )
            )
            continue
        known = [details[i] for i in ids if i in details]
        scores = [s for s in (cvss_base_score(d) for d in known) if s is not None]
        top_score = max(scores) if scores else None
        # Prefer the CVSS band over the database's coarse label: it is present
        # more often and is a published, externally defined scale.
        if top_score is not None:
            worst = severity_from_score(top_score)
        else:
            labels = [severity_of(d) for d in known]
            worst = min(labels, key=SEVERITY_ORDER.index) if labels else "unknown"
        ages = [a for a in (_published_days_ago(d, now) for d in known) if a is not None]
        fixes = [f for f in (fixed_version_of(d) for d in known) if f]
        findings.append(
            AdvisoryFinding(
                ecosystem=pkg.ecosystem,
                name=pkg.name,
                version=pkg.version,
                direct=pkg.direct,
                severity=worst,
                cvss_score=top_score,
                oldest_advisory_days=max(ages) if ages else None,
                advisory_count=len(ids),
                advisory_ids=ids[:10],
                fixed_version=sorted(fixes, key=_version_key)[-1] if fixes else None,
            )
        )
    findings.sort(
        key=lambda f: (SEVERITY_ORDER.index(f.severity), not f.direct, f.name.lower())
    )
    by_severity: dict[str, int] = {}
    for finding in findings:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
    summary = DependencyAdvisories(
        collected=True,
        source="osv",
        scope=scope,
        assessed_package=assessed_package,
        assessed_count=len(packages),
        unassessed_count=skipped,
        affected_count=len(findings),
        direct_affected_count=sum(1 for f in findings if f.direct),
        advisory_count=sum(f.advisory_count for f in findings),
        by_severity=by_severity,
        malicious_count=len(malicious),
        malicious=sorted(malicious, key=lambda m: (not m.direct, m.name.lower())),
    )
    if len(findings) > MAX_FINDINGS_IN_REPORT:
        summary.truncated = True
        findings = findings[:MAX_FINDINGS_IN_REPORT]
    summary.findings = findings
    return summary


def _fetch_details(
    client: httpx.Client, ids: list[str], cache: dict[str, dict[str, Any]], timeout: float
) -> None:
    """Populate ``cache`` with advisory records, concurrently and best-effort.

    Detail lookups are one request per advisory and dominate the step's wall
    clock when the cache is cold — a repository with 38 distinct advisories
    costs ~17s serially. They are independent GETs against a CDN-backed API, so
    a small thread pool collapses that to roughly one round trip. Failures are
    swallowed: a missing record means the finding reports unknown severity, not
    a failed scan.
    """
    def fetch(vuln_id: str) -> None:
        try:
            response = client.get(OSV_VULN_URL.format(vuln_id=vuln_id), timeout=timeout)
            response.raise_for_status()
            cache[vuln_id] = response.json()
        except Exception:
            return

    with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
        list(pool.map(fetch, ids))


# Where to ask whether one exact version is still served. A malicious package
# that the registry has pulled cannot be installed any more, and scoring a
# repository as if it ships live malware would overstate what is true today.
#
# The question is deliberately "is *this version* still there", not "did the
# registry publish a replacement". npm's convention is to leave a
# `x.y.z-security` holding package as `latest`, which protects everyone
# resolving a range — and nobody who pinned the exact bad version. Reading
# `latest` would have cleared repositories that still fetch the artifact on
# every install.
def go_proxy_path(module: str) -> str:
    """Module path as the Go proxy spells it.

    The proxy is served from a case-insensitive filesystem, so an uppercase
    letter is encoded as ``!`` plus its lowercase form. Lowercasing instead —
    the obvious shortcut — answers 404 for every module with a capital in its
    path, which this checker would read as "the registry pulled it" and use to
    clear a live malicious package. Wrong in the one direction that matters.
    """
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), module)


def maven_coordinate_path(name: str) -> str:
    """``group:artifact`` as a Maven Central path: the group's dots are
    directories. Our SBOM parser joins Maven coordinates with ``:``."""
    group, _, artifact = name.partition(":")
    return f"{group.replace('.', '/')}/{artifact}" if artifact else group


# One request per ecosystem, answered by HTTP status alone: 200 means the exact
# version is still served, 404 that it is gone.
#
# **NuGet and Packagist are deliberately absent.** Neither offers a per-version
# endpoint that answers by status. Packagist returns every version of a package
# in one document (80 KB for monolog), and NuGet's per-version URL is the
# ``.nupkg`` itself — 2.4 MB for Newtonsoft.Json. Both would need a different
# shape: fetch a version list and search it. Until that exists they report
# ``None``, which is scored as still published — the safe direction.
_REGISTRY_VERSION_URL: dict[str, str] = {
    "npm": "https://registry.npmjs.org/{name}/{version}",
    "pypi": "https://pypi.org/pypi/{name}/{version}/json",
    "crates": "https://crates.io/api/v1/crates/{name}/{version}",
    "rubygems": "https://rubygems.org/api/v2/rubygems/{name}/versions/{version}.json",
    "hex": "https://hex.pm/api/packages/{name}/releases/{version}",
    "go": "https://proxy.golang.org/{name}/@v/{version}.info",
    "maven": "https://repo1.maven.org/maven2/{name}/{version}/",
}

# Registries whose path spelling differs from the name we resolved.
_REGISTRY_NAME_PATH: dict[str, Callable[[str], str]] = {
    "go": go_proxy_path,
    "maven": maven_coordinate_path,
}


def registry_still_serves(
    client: httpx.Client, ecosystem: str, name: str, version: Optional[str], timeout: float
) -> Optional[bool]:
    """Whether the registry still serves this exact version.

    ``None`` whenever the question could not be answered — an uncovered
    ecosystem, no resolved version, a transport failure, or any status other
    than a clean 200/404. Callers treat ``None`` as "still published", because
    failing to reach a registry is not evidence that malware was withdrawn.
    """
    ecosystem = ecosystem.lower()
    template = _REGISTRY_VERSION_URL.get(ecosystem)
    if not template or not version:
        return None
    path = _REGISTRY_NAME_PATH.get(ecosystem, lambda value: value)(name)
    url = template.format(name=quote(path, safe="@/!"), version=quote(version, safe=""))
    try:
        response = client.get(url, timeout=timeout, follow_redirects=True)
    except Exception:
        return None
    if response.status_code == 404:
        return False
    if response.status_code == 200:
        return True
    return None


def check_still_published(
    client: httpx.Client,
    findings: list[MaliciousDependency],
    timeout: float,
) -> None:
    """Fill in ``still_published`` for each malicious finding, in place.

    One request per finding, and findings are near-always zero or one — this
    costs nothing in the ordinary case and is skipped entirely when the list is
    empty. Best-effort by construction: every failure leaves ``None``.
    """
    for finding in findings:
        finding.still_published = registry_still_serves(
            client, finding.ecosystem, finding.name, finding.version, timeout
        )


def _post_batch(client: httpx.Client, queries: list[dict[str, Any]], timeout: float) -> list[dict]:
    response = client.post(OSV_BATCH_URL, json={"queries": queries}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("OSV batch response had no results list")
    return results


def collect_advisories(
    packages: list[ResolvedDependency],
    warnings: list[str],
    *,
    total_packages: Optional[int] = None,
    scope: str = "repository_graph",
    assessed_package: Optional[str] = None,
    budget_seconds: float = TIME_BUDGET_SECONDS,
    detail_cache: Optional[dict[str, dict[str, Any]]] = None,
    client: Optional[httpx.Client] = None,
) -> DependencyAdvisories:
    """Match resolved dependencies against OSV advisories, best-effort.

    Never raises: every failure fills ``DependencyAdvisories.error``, adds a
    report warning, and leaves ``collected`` False so the metric is excluded
    rather than scored.

    ``detail_cache`` overrides the process-wide advisory cache. Advisory data
    is universal, so the default shared cache makes the steady-state detail
    cost zero across repositories scanned by the same worker; pass an explicit
    dict to isolate a scan (tests do this).
    """
    # The caller may pass a list the report truncated; anything the graph
    # counted but did not embed was never assessed and must be counted as such.
    beyond_list = max(0, (total_packages or len(packages)) - len(packages))
    result = DependencyAdvisories(scope=scope, assessed_package=assessed_package)
    if not packages:
        result.error = "No resolved dependencies to assess"
        return result

    queryable, queries, skipped = build_queries(packages)
    skipped += beyond_list
    if not queries:
        result.error = "No resolved dependencies carried a version and a supported ecosystem"
        result.unassessed_count = skipped
        warnings.append(result.error)
        return result

    deadline = time.monotonic() + budget_seconds
    cache = detail_cache if detail_cache is not None else _PROCESS_DETAIL_CACHE
    if len(cache) > MAX_CACHED_ADVISORIES:
        cache.clear()
    owns_client = client is None
    http = client or httpx.Client(headers={"content-type": "application/json"})
    try:
        results: list[dict] = []
        for start in range(0, len(queries), MAX_BATCH):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.error = "Advisory lookup skipped: time budget exhausted"
                warnings.append(result.error)
                return result
            results.extend(_post_batch(http, queries[start:start + MAX_BATCH], remaining))

        if len(results) != len(queryable):
            result.error = (
                f"OSV returned {len(results)} results for {len(queryable)} queries; "
                "advisory matching skipped"
            )
            warnings.append(result.error)
            return result

        wanted: list[str] = []
        for entry in results:
            for vuln in (entry or {}).get("vulns") or []:
                vid = vuln.get("id") if isinstance(vuln, dict) else None
                if vid and vid not in cache and vid not in wanted:
                    wanted.append(vid)

        # Malicious-package records first. Classification does not depend on
        # the body — the id settles it — but the reported date does, and a
        # repository with a large advisory set must not lose that detail to the
        # cap while ordinary CVEs consume the budget.
        wanted.sort(key=lambda vid: not is_malicious_id(vid))

        if len(wanted) > MAX_DETAIL_LOOKUPS:
            warnings.append(
                f"Advisory severity resolved for {MAX_DETAIL_LOOKUPS} of {len(wanted)} "
                "advisories (lookup cap); the remainder are reported as unknown severity"
            )
            wanted = wanted[:MAX_DETAIL_LOOKUPS]

        if wanted:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                warnings.append(
                    "Advisory severity lookup skipped at the time budget; "
                    "advisories are reported as unknown severity"
                )
            else:
                _fetch_details(http, wanted, cache, remaining)

        summary = summarize(queryable, results, cache, skipped, scope, assessed_package)
        if summary.malicious:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                check_still_published(http, summary.malicious, remaining)
        return summary
    except httpx.HTTPError as exc:
        result.error = f"OSV advisory lookup failed: {exc}"
    except (ValueError, json.JSONDecodeError) as exc:
        result.error = f"OSV advisory response was unusable: {exc}"
    except Exception as exc:  # never let this step break a scan
        result.error = f"Unexpected error during advisory lookup: {exc}"
    finally:
        if owns_client:
            http.close()
    warnings.append(result.error or "OSV advisory lookup failed")
    return result
