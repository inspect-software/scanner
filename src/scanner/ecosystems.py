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
import re
import tomllib
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

import httpx

from .models import Dependency, EcosystemPackage

USER_AGENT = "inspect-scanner (+https://github.com/inspect-software/scanner)"

# Manifest filename -> ecosystem, for exact-name manifests.
SUPPORTED_MANIFESTS: dict[str, str] = {
    "pyproject.toml": "pypi",
    "setup.cfg": "pypi",
    "package.json": "npm",
    "composer.json": "packagist",
    "Cargo.toml": "crates",
    "go.mod": "go",
    "pom.xml": "maven",
    "Gemfile": "rubygems",
    "mix.exs": "hex",
}

# Glob-suffix manifests (filename varies, e.g. MyLib.csproj, mygem.gemspec).
SUPPORTED_MANIFEST_SUFFIXES: dict[str, str] = {
    ".csproj": "nuget",
    ".gemspec": "rubygems",
}

MAX_PACKAGES = 8  # bound registry calls per scan


def ranked_ecosystems(
    packages: Sequence[EcosystemPackage], dependency_ecosystems: Iterable[str]
) -> list[str]:
    """Ecosystems associated with the repository, strongest evidence first.

    A repository can legitimately live in several ecosystems (a Rust core with
    Python bindings and an npm wrapper). When a single "main" ecosystem must be
    named — the first entry here — alphabetical order is misleading, so the
    list is ranked by evidence strength instead:

    1. Ecosystems where the repo *publishes* a package, ordered by combined
       monthly downloads, then total downloads, then name. Packages whose
       registry entry points at a different repository are excluded — the
       manifest names a package this repo does not own (mirrors the scoring
       rule in metrics.py).
    2. Ecosystems seen only in dependency manifests (no published or fetchable
       package), alphabetically.
    """
    published: dict[str, tuple[int, int]] = {}
    for pkg in packages:
        if not pkg.exists or pkg.matches_repo is False:
            continue
        monthly, total = published.get(pkg.ecosystem, (0, 0))
        published[pkg.ecosystem] = (
            monthly + (pkg.monthly_downloads or 0),
            total + (pkg.total_downloads or 0),
        )
    ranked = sorted(
        published, key=lambda eco: (-published[eco][0], -published[eco][1], eco)
    )
    ranked.extend(sorted(set(dependency_ecosystems) - set(ranked)))
    return ranked


def ecosystem_for_manifest(filename: str) -> Optional[str]:
    """Ecosystem for a manifest filename, matching exact names then suffixes."""
    if filename in SUPPORTED_MANIFESTS:
        return SUPPORTED_MANIFESTS[filename]
    for suffix, ecosystem in SUPPORTED_MANIFEST_SUFFIXES.items():
        if filename.endswith(suffix):
            return ecosystem
    return None


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


_GEMSPEC_NAME_RE = re.compile(r"\.name\s*=\s*['\"]([^'\"]+)['\"]")


def parse_gemspec(text: str) -> Optional[str]:
    """Published gem name from a .gemspec (`spec.name = "foo"`)."""
    match = _GEMSPEC_NAME_RE.search(text)
    return match.group(1) if match else None


_MIX_APP_RE = re.compile(r"app:\s*:([A-Za-z0-9_]+)")


def parse_mix_exs(text: str) -> Optional[str]:
    """Published Hex app name from mix.exs (`app: :foo`)."""
    match = _MIX_APP_RE.search(text)
    return match.group(1) if match else None


MANIFEST_PARSERS: dict[str, Callable[[str], Optional[str]]] = {
    "pyproject.toml": parse_pyproject,
    "setup.cfg": parse_setup_cfg,
    "package.json": parse_package_json,
    "composer.json": parse_composer_json,
    "Cargo.toml": parse_cargo_toml,
    "mix.exs": parse_mix_exs,
}

MANIFEST_SUFFIX_PARSERS: dict[str, Callable[[str], Optional[str]]] = {
    ".gemspec": parse_gemspec,
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
        ecosystem = ecosystem_for_manifest(filename)
        parser = _manifest_parser(filename)
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


def _manifest_parser(filename: str) -> Optional[Callable[[str], Optional[str]]]:
    """Published-package-name parser for a manifest filename (exact or suffix)."""
    if filename in MANIFEST_PARSERS:
        return MANIFEST_PARSERS[filename]
    for suffix, parser in MANIFEST_SUFFIX_PARSERS.items():
        if filename.endswith(suffix):
            return parser
    return None


def _dependency_parser(filename: str) -> Optional[Callable[[str], list]]:
    if filename in DEPENDENCY_PARSERS:
        return DEPENDENCY_PARSERS[filename]
    for suffix, parser in DEPENDENCY_SUFFIX_PARSERS.items():
        if filename.endswith(suffix):
            return parser
    return None


# ---------------------------------------------------------------------------
# Dependency-list parsing (pure) — declared dependencies, verbatim, straight
# from the manifest text already fetched for package identification above. No
# registry lookups: freshness and vulnerability checks are not yet performed.
# ---------------------------------------------------------------------------

_PEP508_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")

# Composer platform pseudo-packages: not real dependencies to look up.
_COMPOSER_PLATFORM_PREFIXES = ("php", "ext-", "lib-", "composer")


def _split_pep508(spec: str) -> tuple[str, Optional[str]]:
    """Split a PEP 508 requirement string into (name, version constraint)."""
    spec = spec.split(";", 1)[0].strip()  # drop environment markers
    match = _PEP508_NAME_RE.match(spec)
    if not match:
        return spec, None
    name = match.group(0)
    rest = spec[len(name):].strip()
    if rest.startswith("["):  # drop extras, e.g. requests[security]
        end = rest.find("]")
        rest = rest[end + 1:].strip() if end != -1 else ""
    return name, rest or None


def _poetry_constraint(spec: Any) -> Optional[str]:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return spec.get("version")
    return None


def parse_pyproject_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    data = tomllib.loads(text)
    project = data.get("project")
    if isinstance(project, dict) and isinstance(project.get("dependencies"), list):
        return [_split_pep508(dep) for dep in project["dependencies"] if isinstance(dep, str)]
    poetry_deps = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies")
    if not isinstance(poetry_deps, dict):
        return []
    return [
        (name, _poetry_constraint(spec))
        for name, spec in poetry_deps.items()
        if name.lower() != "python"
    ]


def parse_setup_cfg_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    parser = configparser.ConfigParser()
    parser.read_string(text)
    if not parser.has_option("options", "install_requires"):
        return []
    raw = parser.get("options", "install_requires")
    return [_split_pep508(line) for line in raw.splitlines() if line.strip()]


def parse_package_json_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    data = json.loads(text)
    deps = data.get("dependencies")
    if not isinstance(deps, dict):
        return []
    return [(name, version) for name, version in deps.items()]


def parse_composer_json_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    data = json.loads(text)
    require = data.get("require")
    if not isinstance(require, dict):
        return []
    return [
        (name, version)
        for name, version in require.items()
        if not name.lower().startswith(_COMPOSER_PLATFORM_PREFIXES)
    ]


def parse_cargo_toml_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    data = tomllib.loads(text)
    deps = data.get("dependencies")
    if not isinstance(deps, dict):
        return []
    result: list[tuple[str, Optional[str]]] = []
    for name, spec in deps.items():
        if isinstance(spec, str):
            result.append((name, spec))
        elif isinstance(spec, dict):
            result.append((name, spec.get("version")))
        else:
            result.append((name, None))
    return result


_GOMOD_REQUIRE_RE = re.compile(r"^\s*([^\s]+)\s+(v[^\s]+)")


def parse_go_mod_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    """Go module requires. Handles both `require (...)` blocks and single
    `require x v1` lines; skips `// indirect` transitive deps."""
    result: list[tuple[str, Optional[str]]] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("//") or not line:
            continue
        if in_block:
            if line.startswith(")"):
                in_block = False
                continue
            entry = line
        elif line.startswith("require ("):
            in_block = True
            continue
        elif line.startswith("require "):
            entry = line[len("require "):].strip()
        else:
            continue
        if "// indirect" in entry:
            continue
        match = _GOMOD_REQUIRE_RE.match(entry)
        if match:
            result.append((match.group(1), match.group(2)))
    return result


def parse_pom_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    """Maven <dependency> entries (groupId:artifactId). Test/provided scope
    excluded. Namespace-agnostic (pom's default namespace is stripped)."""
    import xml.etree.ElementTree as ET

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    result: list[tuple[str, Optional[str]]] = []
    for dep in root.iter():
        if local(dep.tag) != "dependency":
            continue
        fields = {local(child.tag): (child.text or "").strip() for child in dep}
        if fields.get("scope") in ("test", "provided", "system"):
            continue
        group = fields.get("groupId")
        artifact = fields.get("artifactId")
        if not artifact:
            continue
        name = f"{group}:{artifact}" if group else artifact
        result.append((name, fields.get("version") or None))
    return result


_GEMFILE_GEM_RE = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]+)['"])?""")
_GEMFILE_GROUP_RE = re.compile(r"^\s*group\s+(.+?)\s+do\b")
_DEV_TEST_GROUPS = ("development", "test")


def parse_gemfile_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    """Ruby `gem 'name', '~> 1.2'` lines. Skips development/test group blocks
    and inline `group:`/`groups:` dev/test declarations."""
    result: list[tuple[str, Optional[str]]] = []
    skip_depth = 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        group_match = _GEMFILE_GROUP_RE.match(line)
        if group_match:
            is_dev_test = any(g in group_match.group(1) for g in _DEV_TEST_GROUPS)
            # track nesting so we only resume gems after the matching `end`
            if skip_depth or is_dev_test:
                skip_depth += 1
            continue
        if skip_depth:
            if re.match(r"^\s*end\b", line):
                skip_depth -= 1
            continue
        match = _GEMFILE_GEM_RE.match(line)
        if not match:
            continue
        rest = line[match.end():]
        if "group" in rest and any(g in rest for g in _DEV_TEST_GROUPS):
            continue
        result.append((match.group(1), match.group(2) or None))
    return result


def parse_csproj_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    """NuGet <PackageReference Include=".." Version=".." /> (attr or child)."""
    import xml.etree.ElementTree as ET

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    result: list[tuple[str, Optional[str]]] = []
    for ref in root.iter():
        if local(ref.tag) != "PackageReference":
            continue
        name = ref.get("Include") or ref.get("Update")
        if not name:
            continue
        version = ref.get("Version")
        if version is None:
            for child in ref:
                if local(child.tag) == "Version":
                    version = (child.text or "").strip() or None
        result.append((name, version))
    return result


_MIX_DEP_RE = re.compile(r"\{:\s*([a-z0-9_]+)\s*,([^}]*)\}")
_MIX_VERSION_RE = re.compile(r'"([^"]+)"')


def parse_mix_dependencies(text: str) -> list[tuple[str, Optional[str]]]:
    """Elixir `{:dep, "~> 1.0"}` tuples. Skips `only: :test`/`:dev` entries."""
    result: list[tuple[str, Optional[str]]] = []
    for match in _MIX_DEP_RE.finditer(text):
        name, rest = match.group(1), match.group(2)
        if "only:" in rest and any(g in rest for g in (":test", ":dev")):
            continue
        version_match = _MIX_VERSION_RE.search(rest)
        result.append((name, version_match.group(1) if version_match else None))
    return result


DEPENDENCY_PARSERS: dict[str, Callable[[str], list[tuple[str, Optional[str]]]]] = {
    "pyproject.toml": parse_pyproject_dependencies,
    "setup.cfg": parse_setup_cfg_dependencies,
    "package.json": parse_package_json_dependencies,
    "composer.json": parse_composer_json_dependencies,
    "Cargo.toml": parse_cargo_toml_dependencies,
    "go.mod": parse_go_mod_dependencies,
    "pom.xml": parse_pom_dependencies,
    "Gemfile": parse_gemfile_dependencies,
    "mix.exs": parse_mix_dependencies,
}

DEPENDENCY_SUFFIX_PARSERS: dict[str, Callable[[str], list[tuple[str, Optional[str]]]]] = {
    ".csproj": parse_csproj_dependencies,
}


def collect_dependencies(manifest_texts: dict[str, str]) -> list[Dependency]:
    """Parse the declared dependency list out of already-fetched manifest text.

    Reports what each manifest declares, verbatim — no registry lookups, no
    deduplication across manifests (a monorepo's package.json and
    pyproject.toml are independent dependency sets)."""
    found: list[Dependency] = []
    for path, text in manifest_texts.items():
        filename = path.rsplit("/", 1)[-1]
        ecosystem = ecosystem_for_manifest(filename)
        parser = _dependency_parser(filename)
        if not ecosystem or not parser:
            continue
        try:
            parsed = parser(text)
        except Exception:
            parsed = []
        for name, constraint in parsed:
            if not name:
                continue
            found.append(
                Dependency(
                    ecosystem=ecosystem, name=name, version_constraint=constraint, manifest=path
                )
            )
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


def map_rubygems(name: str, gem: dict[str, Any], versions: list[dict[str, Any]],
                 repo_full_name: str) -> EcosystemPackage:
    # rubygems /versions returns newest-first with created_at per version.
    created = [_iso(v.get("created_at")) for v in versions if v.get("created_at")]
    licenses = gem.get("licenses") or []
    repo_url = gem.get("source_code_uri") or gem.get("homepage_uri")
    return EcosystemPackage(
        ecosystem="rubygems",
        name=name,
        registry_url=f"https://rubygems.org/gems/{name}",
        latest_version=gem.get("version"),
        latest_published_at=created[0] if created else None,
        days_since_latest_publish=_days_since(created[0]) if created else None,
        first_published_at=created[-1] if created else None,
        versions_count=len(versions) or None,
        total_downloads=gem.get("downloads"),  # rubygems exposes no monthly figure
        license=licenses[0] if licenses else None,
        repository_url=repo_url,
        matches_repo=_repo_matches(repo_url, repo_full_name),
    )


def map_hex(name: str, payload: dict[str, Any], repo_full_name: str) -> EcosystemPackage:
    releases = payload.get("releases") or []
    dated = sorted(
        (r for r in releases if r.get("inserted_at")),
        key=lambda r: r["inserted_at"], reverse=True,
    )
    latest = dated[0] if dated else {}
    downloads = payload.get("downloads") or {}
    recent = downloads.get("recent")
    # hex "recent" is a ~90-day figure; approximate a month.
    monthly = round(recent / 3) if isinstance(recent, (int, float)) else None

    meta = payload.get("meta") or {}
    licenses = meta.get("licenses") or []
    links = meta.get("links") or {}
    repo_url = next(
        (v for k, v in links.items() if "github.com" in (v or "").lower()), None
    )
    retirements = payload.get("retirements") or {}
    latest_retired = latest.get("version") in retirements if latest else False

    return EcosystemPackage(
        ecosystem="hex",
        name=name,
        registry_url=f"https://hex.pm/packages/{name}",
        latest_version=latest.get("version"),
        latest_published_at=_iso(latest.get("inserted_at")),
        days_since_latest_publish=_days_since(_iso(latest.get("inserted_at"))),
        first_published_at=_iso(dated[-1].get("inserted_at")) if dated else None,
        versions_count=len(releases) or None,
        monthly_downloads=monthly,
        total_downloads=downloads.get("all"),
        license=licenses[0] if licenses else None,
        is_deprecated=bool(latest_retired),
        deprecation_note="retired release" if latest_retired else None,
        repository_url=repo_url,
        matches_repo=_repo_matches(repo_url, repo_full_name),
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


def fetch_rubygems(client: httpx.Client, name: str, repo_full_name: str) -> Optional[EcosystemPackage]:
    gem = _get_json(client, f"https://rubygems.org/api/v1/gems/{name}.json")
    if not isinstance(gem, dict):
        return None
    versions = _get_json(client, f"https://rubygems.org/api/v1/versions/{name}.json")
    return map_rubygems(name, gem, versions if isinstance(versions, list) else [], repo_full_name)


def fetch_hex(client: httpx.Client, name: str, repo_full_name: str) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://hex.pm/api/packages/{name}")
    if not isinstance(payload, dict):
        return None
    return map_hex(name, payload, repo_full_name)


FETCHERS: dict[str, Callable[[httpx.Client, str, str], Optional[EcosystemPackage]]] = {
    "pypi": fetch_pypi,
    "npm": fetch_npm,
    "packagist": fetch_packagist,
    "crates": fetch_crates,
    "rubygems": fetch_rubygems,
    "hex": fetch_hex,
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
        if ecosystem_for_manifest(path.rsplit("/", 1)[-1]):
            out.append(path)
    return out


def collect_ecosystem(
    owner: str, repo: str, branch: Optional[str], tree_paths: list[str],
    warnings: list[str],
) -> tuple[list[EcosystemPackage], list[Dependency]]:
    """Identify the repo's published packages and declared dependencies.

    Reads manifests already fetched from the repo (raw.githubusercontent): the
    declared package name feeds registry lookups (``EcosystemPackage``); the
    declared dependency list (``Dependency``) is reported as parsed, with no
    registry lookups, freshness checks, or vulnerability scanning.
    """
    paths = manifest_paths(tree_paths)
    if not paths or not branch:
        return [], []
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
        packages = _fetch_packages(client, identified, repo_full_name, warnings) if identified else []
        dependencies = collect_dependencies(texts)
        return packages, dependencies
