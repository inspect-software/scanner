"""Package-ecosystem adapters: identify a repo's published package(s) and
fetch registry facts (versions, publish recency, downloads, deprecation).

Supported registries: PyPI, npm, Packagist, crates.io. Each adapter has two
halves kept deliberately separate:

- **parse_*(text)** — pure: extract the package id from a manifest file.
- **map_*(...)** — pure: turn a registry JSON payload into an EcosystemPackage.
- **fetch_*(client, name, repo)** — network: call the registry, then map.

The pure halves are unit-tested without touching the network. All network
failures degrade to ``None`` + a warning; a missing registry never aborts a
scan.
"""

from __future__ import annotations

import configparser
import json
import tomllib
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from .models import EcosystemPackage

USER_AGENT = "inspect-scanner (+https://github.com/inspect-software/scanner)"

# Manifest filename -> ecosystem. Only registries we actually integrate.
SUPPORTED_MANIFESTS: dict[str, str] = {
    "pyproject.toml": "pypi",
    "setup.cfg": "pypi",
    "package.json": "npm",
    "composer.json": "packagist",
    "Cargo.toml": "crates",
}

MAX_PACKAGES = 8  # bound registry calls per scan


# ---------------------------------------------------------------------------
# Manifest parsing (pure)
# ---------------------------------------------------------------------------


def parse_pyproject(text: str) -> Optional[str]:
    data = tomllib.loads(text)
    project = data.get("project")
    if isinstance(project, dict) and project.get("name"):
        return project["name"]
    poetry = (data.get("tool") or {}).get("poetry") or {}
    return poetry.get("name")


def parse_setup_cfg(text: str) -> Optional[str]:
    parser = configparser.ConfigParser()
    parser.read_string(text)
    if parser.has_option("metadata", "name"):
        return parser.get("metadata", "name").strip() or None
    return None


def parse_package_json(text: str) -> Optional[str]:
    data = json.loads(text)
    if data.get("private") is True:
        return None  # explicitly not published
    return data.get("name") or None


def parse_composer_json(text: str) -> Optional[str]:
    data = json.loads(text)
    return data.get("name") or None


def parse_cargo_toml(text: str) -> Optional[str]:
    data = tomllib.loads(text)
    package = data.get("package")
    if isinstance(package, dict):
        return package.get("name")
    return None


MANIFEST_PARSERS: dict[str, Callable[[str], Optional[str]]] = {
    "pyproject.toml": parse_pyproject,
    "setup.cfg": parse_setup_cfg,
    "package.json": parse_package_json,
    "composer.json": parse_composer_json,
    "Cargo.toml": parse_cargo_toml,
}


def identify_packages(manifest_texts: dict[str, str]) -> list[tuple[str, str]]:
    """Map {manifest_path: content} -> unique [(ecosystem, package_name)].

    Only root or one-level-deep manifests should be passed in (the caller
    filters); vendored deep manifests would produce false positives.
    """
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path, text in manifest_texts.items():
        filename = path.rsplit("/", 1)[-1]
        ecosystem = SUPPORTED_MANIFESTS.get(filename)
        parser = MANIFEST_PARSERS.get(filename)
        if not ecosystem or not parser:
            continue
        try:
            name = parser(text)
        except Exception:
            name = None
        if not name:
            continue
        key = (ecosystem, name.lower())
        if key not in seen:
            seen.add(key)
            found.append((ecosystem, name))
    return found


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_since(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def _repo_matches(repository_url: Optional[str], repo_full_name: str) -> Optional[bool]:
    if not repository_url:
        return None
    return repo_full_name.lower() in repository_url.lower().replace(".git", "")


# ---------------------------------------------------------------------------
# Response mapping (pure)
# ---------------------------------------------------------------------------


def map_pypi(name: str, payload: dict[str, Any], last_month: Optional[int],
             repo_full_name: str) -> EcosystemPackage:
    info = payload.get("info") or {}
    releases = payload.get("releases") or {}
    latest = info.get("version")

    upload_times = []
    for files in releases.values():
        for f in files or []:
            dt = _iso(f.get("upload_time_iso_8601") or f.get("upload_time"))
            if dt:
                upload_times.append(dt)
    latest_files = releases.get(latest) or []
    latest_dt = max(
        (_iso(f.get("upload_time_iso_8601") or f.get("upload_time")) for f in latest_files),
        default=None,
    )

    urls = info.get("project_urls") or {}
    repo_url = _pick_repo_url(urls, info.get("home_page"))
    license_ = info.get("license_expression") or _short_license(info.get("license"))

    return EcosystemPackage(
        ecosystem="pypi",
        name=name,
        registry_url=f"https://pypi.org/project/{name}/",
        latest_version=latest,
        latest_published_at=latest_dt,
        days_since_latest_publish=_days_since(latest_dt),
        first_published_at=min(upload_times) if upload_times else None,
        versions_count=len([v for v in releases if releases[v]]),
        monthly_downloads=last_month,
        license=license_,
        latest_version_yanked=any(f.get("yanked") for f in latest_files) or None,
        repository_url=repo_url,
        matches_repo=_repo_matches(repo_url, repo_full_name),
    )


def map_npm(name: str, payload: dict[str, Any], last_month: Optional[int],
            repo_full_name: str) -> EcosystemPackage:
    dist_tags = payload.get("dist-tags") or {}
    latest = dist_tags.get("latest")
    versions = payload.get("versions") or {}
    times = payload.get("time") or {}
    latest_meta = versions.get(latest) or {}

    repo = payload.get("repository") or latest_meta.get("repository") or {}
    repo_url = repo.get("url") if isinstance(repo, dict) else (repo or None)
    license_ = _short_license(payload.get("license") or latest_meta.get("license"))
    deprecated = latest_meta.get("deprecated")

    return EcosystemPackage(
        ecosystem="npm",
        name=name,
        registry_url=f"https://www.npmjs.com/package/{name}",
        latest_version=latest,
        latest_published_at=_iso(times.get(latest)),
        days_since_latest_publish=_days_since(_iso(times.get(latest))),
        first_published_at=_iso(times.get("created")),
        versions_count=len(versions),
        monthly_downloads=last_month,
        license=license_,
        maintainers_count=len(payload.get("maintainers") or []) or None,
        is_deprecated=bool(deprecated),
        deprecation_note=deprecated if isinstance(deprecated, str) else None,
        repository_url=repo_url,
        matches_repo=_repo_matches(repo_url, repo_full_name),
    )


def map_packagist(name: str, payload: dict[str, Any], repo_full_name: str) -> EcosystemPackage:
    pkg = payload.get("package") or {}
    versions = pkg.get("versions") or {}
    stable = _latest_stable_packagist(versions)
    latest_meta = versions.get(stable) or {}
    downloads = pkg.get("downloads") or {}
    abandoned = pkg.get("abandoned")

    license_list = latest_meta.get("license") or []
    return EcosystemPackage(
        ecosystem="packagist",
        name=name,
        registry_url=f"https://packagist.org/packages/{name}",
        latest_version=stable,
        latest_published_at=_iso(latest_meta.get("time")),
        days_since_latest_publish=_days_since(_iso(latest_meta.get("time"))),
        versions_count=len([v for v in versions if "dev" not in v.lower()]) or None,
        monthly_downloads=downloads.get("monthly"),
        total_downloads=downloads.get("total"),
        dependents_count=pkg.get("dependents"),
        license=license_list[0] if license_list else None,
        is_deprecated=bool(abandoned),
        deprecation_note=(abandoned if isinstance(abandoned, str) else None),
        repository_url=pkg.get("repository"),
        matches_repo=_repo_matches(pkg.get("repository"), repo_full_name),
    )


def map_crates(name: str, payload: dict[str, Any], repo_full_name: str) -> EcosystemPackage:
    crate = payload.get("crate") or {}
    versions = payload.get("versions") or []
    latest_meta = versions[0] if versions else {}
    recent = crate.get("recent_downloads")
    # crates.io "recent_downloads" is a ~90-day figure; approximate a month.
    monthly = round(recent / 3) if isinstance(recent, (int, float)) else None

    return EcosystemPackage(
        ecosystem="crates",
        name=name,
        registry_url=f"https://crates.io/crates/{name}",
        latest_version=crate.get("max_stable_version") or crate.get("newest_version"),
        latest_published_at=_iso(latest_meta.get("created_at")),
        days_since_latest_publish=_days_since(_iso(latest_meta.get("created_at"))),
        first_published_at=_iso(crate.get("created_at")),
        versions_count=len(versions) or None,
        monthly_downloads=monthly,
        total_downloads=crate.get("downloads"),
        license=latest_meta.get("license"),
        latest_version_yanked=latest_meta.get("yanked"),
        repository_url=crate.get("repository"),
        matches_repo=_repo_matches(crate.get("repository"), repo_full_name),
    )


def _pick_repo_url(project_urls: dict[str, Any], home_page: Optional[str]) -> Optional[str]:
    for key in ("Source", "Repository", "Source Code", "Code", "GitHub", "Homepage"):
        for k, v in project_urls.items():
            if k.lower() == key.lower() and "github.com" in (v or "").lower():
                return v
    for v in project_urls.values():
        if "github.com" in (v or "").lower():
            return v
    return home_page


def _short_license(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    # PyPI sometimes stuffs the full license text into this field.
    return value if len(value) <= 60 else None


def _latest_stable_packagist(versions: dict[str, Any]) -> Optional[str]:
    stable = [v for v in versions if "dev" not in v.lower()]
    return (stable or list(versions))[0] if versions else None


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------


def _get_json(client: httpx.Client, url: str) -> Optional[Any]:
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _get_text(client: httpx.Client, url: str) -> Optional[str]:
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        return None
    return resp.text if resp.status_code == 200 else None


def _get_int(client: httpx.Client, url: str, *keys: str) -> Optional[int]:
    data = _get_json(client, url)
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, int) else None


def fetch_pypi(client: httpx.Client, name: str, repo_full_name: str) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://pypi.org/pypi/{name}/json")
    if not payload:
        return None
    last_month = _get_int(
        client, f"https://pypistats.org/api/packages/{name.lower()}/recent", "data", "last_month"
    )
    return map_pypi(name, payload, last_month, repo_full_name)


def fetch_npm(client: httpx.Client, name: str, repo_full_name: str) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://registry.npmjs.org/{name}")
    if not payload:
        return None
    last_month = _get_int(
        client, f"https://api.npmjs.org/downloads/point/last-month/{name}", "downloads"
    )
    return map_npm(name, payload, last_month, repo_full_name)


def fetch_packagist(client: httpx.Client, name: str, repo_full_name: str) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://packagist.org/packages/{name}.json")
    if not payload:
        return None
    return map_packagist(name, payload, repo_full_name)


def fetch_crates(client: httpx.Client, name: str, repo_full_name: str) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://crates.io/api/v1/crates/{name}")
    if not payload:
        return None
    return map_crates(name, payload, repo_full_name)


FETCHERS: dict[str, Callable[[httpx.Client, str, str], Optional[EcosystemPackage]]] = {
    "pypi": fetch_pypi,
    "npm": fetch_npm,
    "packagist": fetch_packagist,
    "crates": fetch_crates,
}


def _fetch_packages(
    client: httpx.Client, packages: list[tuple[str, str]], repo_full_name: str,
    warnings: list[str],
) -> list[EcosystemPackage]:
    results: list[EcosystemPackage] = []
    for ecosystem, name in packages[:MAX_PACKAGES]:
        fetcher = FETCHERS.get(ecosystem)
        if not fetcher:
            continue
        try:
            pkg = fetcher(client, name, repo_full_name)
        except Exception:
            pkg = None
        if pkg is None:
            warnings.append(f"Could not fetch {ecosystem} package '{name}' from its registry")
            continue
        if pkg.matches_repo is False:
            warnings.append(
                f"{ecosystem} package '{name}' points at a different repository "
                f"({pkg.repository_url}); excluded from ecosystem scoring"
            )
        results.append(pkg)
    return results


def manifest_paths(tree_paths: list[str]) -> list[str]:
    """Root or one-level-deep manifest files for supported ecosystems."""
    out = []
    for path in tree_paths:
        if path.count("/") > 1:
            continue
        if path.rsplit("/", 1)[-1] in SUPPORTED_MANIFESTS:
            out.append(path)
    return out


def collect_ecosystem(
    owner: str, repo: str, branch: Optional[str], tree_paths: list[str],
    warnings: list[str],
) -> list[EcosystemPackage]:
    """Identify the repo's published packages and fetch their registry facts.

    Reads the declared package name straight from the repo's own manifests
    (raw.githubusercontent), then queries each ecosystem registry.
    """
    paths = manifest_paths(tree_paths)
    if not paths or not branch:
        return []
    repo_full_name = f"{owner}/{repo}"
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        texts: dict[str, str] = {}
        for path in paths:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            text = _get_text(client, url)
            if text is not None:
                texts[path] = text
        identified = identify_packages(texts)
        if not identified:
            return []
        return _fetch_packages(client, identified, repo_full_name, warnings)
