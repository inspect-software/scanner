"""Package-ecosystem adapters: identify a repo's published package(s) and
fetch registry facts (versions, publish recency, downloads, deprecation).

Supported registries: PyPI, npm, Packagist, crates.io, RubyGems, Hex, the Go
module proxy, Maven Central, and NuGet. Each adapter has two halves kept
deliberately separate:

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
import time
import tomllib
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

import httpx

from .contacts import dedupe as dedupe_contacts
from .contacts import (
    from_crates as contacts_from_crates,
    from_hex as contacts_from_hex,
    from_npm as contacts_from_npm,
    from_packagist as contacts_from_packagist,
    from_pypi as contacts_from_pypi,
    from_rubygems as contacts_from_rubygems,
)
from .models import ContactChannel, Dependency, EcosystemPackage
from .repourl import display_repository_url, npm_source_url, url_of

USER_AGENT = "inspect-scanner (+https://github.com/inspect-software/scanner)"

# Manifest filename -> ecosystem, for exact-name manifests.
SUPPORTED_MANIFESTS: dict[str, str] = {
    "pyproject.toml": "pypi",
    "setup.cfg": "pypi",
    "setup.py": "pypi",
    "package.json": "npm",
    "composer.json": "packagist",
    "Cargo.toml": "crates",
    "go.mod": "go",
    "pom.xml": "maven",
    "Gemfile": "rubygems",
    "mix.exs": "hex",
    "gleam.toml": "hex",
    # Erlang: rebar.config marks a rebar3 project but never carries the
    # published name — that lives in src/<app>.app.src. Listed anyway so the
    # file is fetched, because a rebar project with no .app.src falls back to
    # the repository name (see speculative_packages).
    "rebar.config": "hex",
}

# Glob-suffix manifests (filename varies, e.g. MyLib.csproj, mygem.gemspec).
SUPPORTED_MANIFEST_SUFFIXES: dict[str, str] = {
    ".csproj": "nuget",
    ".gemspec": "rubygems",
    ".app.src": "hex",
}

MAX_PACKAGES = 8  # bound registry calls per scan

# Bound file fetches when falling back to nested manifests. Monorepos reach
# into the hundreds (114 in a production sample) and every one costs a raw
# fetch, so the fallback takes a prefix rather than the whole set.
MAX_NESTED_MANIFESTS = 20

# Directory segments that hold sample, test or vendored projects rather than
# the thing the repository publishes. Filtering these is not just cost control:
# in a production sample the noise manifests crowded real packages out of the
# MAX_PACKAGES budget, so the filter raises the resolution rate as well.
NOISE_PATH_SEGMENTS = frozenset({
    "test", "tests", "sample", "samples", "example", "examples",
    "benchmark", "benchmarks", "fuzz", "fuzzing", "doc", "docs",
    "vendor", "third_party", "thirdparty", "demo", "demos",
})


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


_SETUP_PY_NAME_RE = re.compile(
    r"""\bname\s*=\s*['"]([A-Za-z0-9][A-Za-z0-9._-]*)['"]"""
)


def parse_setup_py(text: str) -> Optional[str]:
    """Package name from a legacy setup.py (`name='flatbuffers'`). setup.py is
    code, not data — a keyword-argument regex covers the overwhelmingly common
    literal form; dynamically built names are simply not identified."""
    match = _SETUP_PY_NAME_RE.search(text)
    return match.group(1) if match else None


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


def parse_gleam_toml(text: str) -> Optional[str]:
    """Published Hex package name from gleam.toml (`name = "lustre"`)."""
    data = tomllib.loads(text)
    name = data.get("name")
    return name if isinstance(name, str) and name else None


_APP_SRC_NAME_RE = re.compile(r"\{\s*application\s*,\s*'?([a-zA-Z][a-zA-Z0-9_]*)'?")


def parse_app_src(text: str) -> Optional[str]:
    """Erlang OTP application name from `src/<app>.app.src`.

    rebar3 publishes an Erlang application to Hex under the name declared in
    this file's `{application, Name, [...]}` term, so it — not rebar.config —
    is where the published name lives. Generated `.app.src` templates leave a
    `'$APP'`-style placeholder there; those simply fail the name pattern.
    """
    match = _APP_SRC_NAME_RE.search(text)
    return match.group(1) if match else None


_GO_MODULE_RE = re.compile(r'^module\s+"?([^\s"]+)"?', re.MULTILINE)


def parse_go_mod(text: str) -> Optional[str]:
    """Published module path from go.mod (`module github.com/x/y`).

    Go has no central publish step — a tagged module under a real hosting
    path *is* published. Paths without a dotted host (e.g. `module demo`)
    are local-only and can never resolve on the proxy."""
    match = _GO_MODULE_RE.search(text)
    if not match:
        return None
    module = match.group(1)
    return module if "." in module.split("/", 1)[0] else None


def parse_pom(text: str) -> Optional[str]:
    """Maven coordinates (`groupId:artifactId`) a pom publishes. The groupId
    may be inherited from `<parent>`; property placeholders can't be resolved
    without a full Maven model, so those poms are skipped.

    ``<packaging>pom</packaging>`` artifacts — BOMs, parents and reactor
    aggregators — are skipped too. They ship no code: nobody installs
    `keycloak-bom-parent` or `zipkin-parent`, they exist to coordinate a build.
    Counting them as published packages both misdescribes the project and, in a
    multi-module repository, crowds the real artifact out of the package
    budget: every one of Keycloak's reported packages was a parent pom.
    """
    import xml.etree.ElementTree as ET

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if local(root.tag) != "project":
        return None
    group = artifact = parent_group = None
    for child in root:
        tag = local(child.tag)
        if tag == "groupId":
            group = (child.text or "").strip() or None
        elif tag == "artifactId":
            artifact = (child.text or "").strip() or None
        elif tag == "packaging":
            if (child.text or "").strip().lower() == "pom":
                return None
        elif tag == "parent":
            for sub in child:
                if local(sub.tag) == "groupId":
                    parent_group = (sub.text or "").strip() or None
    group = group or parent_group
    if not group or not artifact or "${" in group + artifact:
        return None
    return f"{group}:{artifact}"


def parse_csproj(text: str) -> Optional[str]:
    """Published NuGet id from a .csproj — explicit `<PackageId>` only. Most
    projects never declare one (the id defaults to the project filename), and
    guessing would query the registry for every internal project."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "PackageId":
            value = (el.text or "").strip()
            if value and "$(" not in value:
                return value
    return None


MANIFEST_PARSERS: dict[str, Callable[[str], Optional[str]]] = {
    "pyproject.toml": parse_pyproject,
    "setup.cfg": parse_setup_cfg,
    "setup.py": parse_setup_py,
    "package.json": parse_package_json,
    "composer.json": parse_composer_json,
    "Cargo.toml": parse_cargo_toml,
    "mix.exs": parse_mix_exs,
    "go.mod": parse_go_mod,
    "pom.xml": parse_pom,
    "gleam.toml": parse_gleam_toml,
}

MANIFEST_SUFFIX_PARSERS: dict[str, Callable[[str], Optional[str]]] = {
    ".gemspec": parse_gemspec,
    ".csproj": parse_csproj,
    ".app.src": parse_app_src,
}


def manifest_rank(path: str) -> tuple[int, int, str]:
    """Sort key placing a repository's principal manifest first.

    The package budget (``MAX_PACKAGES``) is small and multi-module
    repositories blow straight through it — Presto publishes 123 manifests,
    Arthas 25. Taking them in tree order meant taking them alphabetically,
    which is uncorrelated with importance and quietly dropped the flagship:
    Rails reported `actioncable` but not `rails`, Zipkin reported
    `zipkin-parent` but not `zipkin`, and Arthas spent three of its eight slots
    on demo and integration-test modules.

    Depth first (a root manifest is the project's own), then path length, which
    stands in for how central a module is — core modules get short directory
    names (`core/`, `boot/`) while peripheral ones get long descriptive ones
    (`arthas-demo-external-command/`). It is a heuristic, but a far better one
    than the alphabet.
    """
    return (path.count("/"), len(path), path)


def identify_packages(manifest_texts: dict[str, str]) -> list[tuple[str, str]]:
    """Map {manifest_path: content} -> unique [(ecosystem, package_name)],
    most likely to be the repository's principal package first.

    Only root or one-level-deep manifests should be passed in (the caller
    filters); vendored deep manifests would produce false positives.
    """
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(manifest_texts, key=manifest_rank):
        text = manifest_texts[path]
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


def speculative_packages(
    manifest_texts: dict[str, str], repo_full_name: str
) -> list[tuple[str, str]]:
    """Guessed package names for manifests that declare none, as (ecosystem, name).

    Two conventions carry the published name outside the manifest body:

    - **.csproj** — `<PackageId>` is optional, and most projects omit it; NuGet
      then defaults the id to the project filename.
    - **rebar.config** — carries no name at all, and a rebar project without a
      `src/<app>.app.src` conventionally publishes under its repository name.

    These are guesses, not declarations, so the caller must only accept one
    whose registry entry points back at this repository (``matches_repo`` is
    True). Without that check a project named after a popular package would
    silently adopt a stranger's download counts.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    repo_name = repo_full_name.rsplit("/", 1)[-1]
    has_app_src = any(p.endswith(".app.src") for p in manifest_texts)

    for path, text in sorted(manifest_texts.items()):
        filename = path.rsplit("/", 1)[-1]
        if filename.endswith(".csproj"):
            try:
                declared = parse_csproj(text)
            except Exception:
                declared = None
            if declared:
                continue  # already identified for real
            name = filename[: -len(".csproj")]
            key = ("nuget", name.lower())
        elif filename == "rebar.config" and not has_app_src:
            name = repo_name
            key = ("hex", name.lower())
        else:
            continue
        if name and key not in seen:
            seen.add(key)
            candidates.append((key[0], name))
    return candidates


def private_manifest_candidates(manifest_texts: dict[str, str]) -> list[tuple[str, str]]:
    """Names declared by ``private: true`` package.json manifests.

    ``private`` normally means unpublished, and ``parse_package_json`` rightly
    skips it — but monorepo release tooling routinely strips the flag at
    publish time, so the name may be very much on the registry. Measured:
    apollographql/apollo-client's workspace root declares
    ``"@apollo/client", "private": true`` while npm's entry for that name —
    ~25M downloads/month — points straight back at the repository. Skipping
    the flag outright cost the report the monorepo's flagship package while a
    satellite package carried the adoption metric.

    These are candidates, not identifications: the caller must feed them
    through the registry with ``require_repo_match=True``, so a genuinely
    private name that happens to collide with a stranger's package is dropped
    by the same rule that guards filename guesses.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path, text in sorted(manifest_texts.items()):
        if path.rsplit("/", 1)[-1] != "package.json":
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        name = data.get("name")
        if data.get("private") is True and isinstance(name, str) and name:
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(("npm", name))
    return out


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


def _declared_url(value: Any) -> Optional[str]:
    """A registry-declared URL, or None for anything that isn't an http(s)
    link — registries pass these fields through from manifests unvalidated."""
    if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
        return value.strip()
    return None


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

    keywords = _split_keywords(info.get("keywords"))
    keywords += [c for c in (info.get("classifiers") or []) if isinstance(c, str)]

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
        documentation_url=next(
            (
                _declared_url(v)
                for k, v in urls.items()
                if k.lower() in ("documentation", "docs", "doc") and _declared_url(v)
            ),
            None,
        ),
        homepage_url=_declared_url(info.get("home_page"))
        or next(
            (
                _declared_url(v)
                for k, v in urls.items()
                if k.lower() in ("homepage", "home") and _declared_url(v)
            ),
            None,
        ),
        keywords=keywords,
    )


def map_npm(name: str, payload: dict[str, Any], last_month: Optional[int],
            repo_full_name: str) -> EcosystemPackage:
    dist_tags = payload.get("dist-tags") or {}
    latest = dist_tags.get("latest")
    versions = payload.get("versions") or {}
    times = payload.get("time") or {}
    latest_meta = versions.get(latest) or {}

    # Same extraction the catalog uses, so a package cannot resolve to one URL
    # here and another there. Falling back to the raw repository field keeps
    # non-GitHub hosts visible in the report; only the catalog needs GitHub.
    raw_repo = payload.get("repository") or latest_meta.get("repository")
    repo_url = npm_source_url(latest_meta, payload) or display_repository_url(url_of(raw_repo))
    license_ = _short_license(payload.get("license") or latest_meta.get("license"))
    deprecated = latest_meta.get("deprecated")
    keywords = payload.get("keywords") or latest_meta.get("keywords")

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
        homepage_url=_declared_url(
            payload.get("homepage") or latest_meta.get("homepage")
        ),
        keywords=_split_keywords(keywords),
    )


def map_packagist(name: str, payload: dict[str, Any], repo_full_name: str) -> EcosystemPackage:
    pkg = payload.get("package") or {}
    versions = pkg.get("versions") or {}
    stable = _latest_stable_packagist(versions)
    latest_meta = versions.get(stable) or {}
    downloads = pkg.get("downloads") or {}
    abandoned = pkg.get("abandoned")

    license_list = latest_meta.get("license") or []
    keywords = pkg.get("keywords") or latest_meta.get("keywords")
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
        homepage_url=_declared_url(latest_meta.get("homepage")),
        keywords=_split_keywords(keywords),
        # Packagist is the one registry that publishes the artifact type as a
        # first-class field, and its vocabulary is controlled.
        declared_type=(pkg.get("type") or latest_meta.get("type")),
    )


def map_crates(name: str, payload: dict[str, Any], repo_full_name: str) -> EcosystemPackage:
    crate = payload.get("crate") or {}
    versions = payload.get("versions") or []
    latest_meta = versions[0] if versions else {}
    recent = crate.get("recent_downloads")
    # crates.io "recent_downloads" is a ~90-day figure; approximate a month.
    monthly = round(recent / 3) if isinstance(recent, (int, float)) else None
    # Categories stay in ``keywords`` for the catalogue vocabulary and are also
    # reported separately: unlike keywords they come from a controlled list, so
    # classification can rely on them.
    categories = [c for c in (crate.get("categories") or []) if isinstance(c, str)]
    keywords = list(crate.get("keywords") or []) + categories

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
        documentation_url=_declared_url(crate.get("documentation")),
        homepage_url=_declared_url(crate.get("homepage")),
        keywords=keywords,
        categories=categories,
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
        documentation_url=_declared_url(gem.get("documentation_uri")),
        homepage_url=_declared_url(gem.get("homepage_uri")),
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
        documentation_url=next(
            (
                _declared_url(v)
                for k, v in links.items()
                if k.lower() in ("docs", "documentation", "doc") and _declared_url(v)
            ),
            None,
        ),
        homepage_url=next(
            (
                _declared_url(v)
                for k, v in links.items()
                if k.lower() in ("homepage", "website", "home") and _declared_url(v)
            ),
            None,
        ),
    )


_GO_DEPRECATED_RE = re.compile(r"^//\s*Deprecated:\s*(.*)$", re.MULTILINE)


def map_go(name: str, latest: dict[str, Any], versions: Sequence[str],
           mod_text: Optional[str], repo_full_name: str) -> EcosystemPackage:
    """Go module facts from the module proxy. The proxy publishes no download
    statistics — Go has none — so the package feeds maintenance metrics only.
    The module path *is* the import path: for a github.com module it names its
    own repository, so `matches_repo` verifies the manifest wasn't vendored in
    from another project."""
    published = _iso(latest.get("Time"))
    deprecated = _GO_DEPRECATED_RE.search(mod_text or "")
    repo_url = (
        "https://" + "/".join(name.split("/")[:3])
        if name.startswith("github.com/") else None
    )
    return EcosystemPackage(
        ecosystem="go",
        name=name,
        registry_url=f"https://pkg.go.dev/{name}",
        latest_version=latest.get("Version"),
        latest_published_at=published,
        days_since_latest_publish=_days_since(published),
        versions_count=len(versions) or None,
        is_deprecated=bool(deprecated),
        deprecation_note=(deprecated.group(1).strip() or None) if deprecated else None,
        repository_url=repo_url,
        matches_repo=_repo_matches(repo_url, repo_full_name),
    )


def parse_maven_metadata(xml_text: str) -> Optional[dict[str, Any]]:
    """Versions and last-publish time from Maven Central's maven-metadata.xml.
    Returns {"latest", "versions", "last_updated"} or None when unparseable."""
    import xml.etree.ElementTree as ET

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    latest = release = last_updated = None
    versions: list[str] = []
    for el in root.iter():
        tag = local(el.tag)
        text = (el.text or "").strip()
        if tag == "latest":
            latest = text or None
        elif tag == "release":
            release = text or None
        elif tag == "version":
            if text:
                versions.append(text)
        elif tag == "lastUpdated" and text:
            try:
                last_updated = datetime.strptime(text, "%Y%m%d%H%M%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                last_updated = None
    version = release or latest or (versions[-1] if versions else None)
    if not version:
        return None
    return {"latest": version, "versions": versions, "last_updated": last_updated}


def _pom_scm_and_license(pom_xml: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(github repo URL, license name) declared in a pom, either possibly None."""
    import xml.etree.ElementTree as ET

    if not pom_xml:
        return None, None
    try:
        root = ET.fromstring(pom_xml)
    except ET.ParseError:
        return None, None
    repo_url = None
    for node in root.findall(".//{*}scm/{*}url") + root.findall(".//{*}scm/{*}connection"):
        value = (node.text or "").strip()
        value = value.replace("scm:git:", "").replace("git@github.com:", "https://github.com/")
        if "github.com" in value.lower():
            repo_url = value
            break
    license_node = root.find(".//{*}licenses/{*}license/{*}name")
    license_ = (license_node.text or "").strip() if license_node is not None else None
    return repo_url, _short_license(license_)


def _pom_packaging(pom_xml: Optional[str]) -> Optional[str]:
    """Maven's ``<packaging>``: jar, war, pom, maven-plugin, ear.

    A first-class artifact-type declaration — a war is a deployable web
    application and a maven-plugin extends a host build, neither of which is a
    dependency anyone can add. Defaults to jar when absent, which says nothing,
    so absence is reported as absence.
    """
    import xml.etree.ElementTree as ET

    if not pom_xml:
        return None
    try:
        root = ET.fromstring(pom_xml)
    except ET.ParseError:
        return None
    node = root.find("{*}packaging")
    value = (node.text or "").strip() if node is not None else ""
    return value or None


def map_maven(name: str, metadata: dict[str, Any], pom_xml: Optional[str],
              repo_full_name: str) -> EcosystemPackage:
    """Maven Central facts. Central publishes no download counter of any kind,
    so the artifact feeds maintenance metrics only. `lastUpdated` in the
    artifact metadata moves on each publish and stands in for the latest
    version's publish time."""
    group, artifact = name.split(":", 1)
    repo_url, license_ = _pom_scm_and_license(pom_xml)
    last_updated = metadata.get("last_updated")
    return EcosystemPackage(
        ecosystem="maven",
        name=name,
        registry_url=f"https://central.sonatype.com/artifact/{group}/{artifact}",
        latest_version=metadata.get("latest"),
        latest_published_at=last_updated,
        days_since_latest_publish=_days_since(last_updated),
        versions_count=len(metadata.get("versions") or []) or None,
        license=license_,
        repository_url=repo_url,
        matches_repo=_repo_matches(repo_url, repo_full_name),
        declared_type=_pom_packaging(pom_xml),
    )


def _nuget_package_type(
    search: dict[str, Any], catalog_entry: Optional[dict[str, Any]]
) -> Optional[str]:
    """NuGet's declared package type, or None for a plain dependency.

    Every package carries the implicit type "Dependency"; recording it would
    say nothing. ``DotnetTool`` and ``Template``, on the other hand, are the
    registry stating outright that the artifact is a command or a scaffold
    rather than something you reference from code.
    """
    for source in (catalog_entry or {}, search):
        entries = source.get("packageTypes")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            declared = entry.get("name") if isinstance(entry, dict) else entry
            if isinstance(declared, str) and declared.lower() != "dependency":
                return declared
    return None


def map_nuget(name: str, search: dict[str, Any], catalog_entry: Optional[dict[str, Any]],
              repo_full_name: str) -> EcosystemPackage:
    """NuGet facts from the v3 search service, with the latest version's
    registration catalogEntry (when resolvable) supplying the publish date and
    deprecation. NuGet reports lifetime downloads only — no monthly figure —
    so adoption falls back to `total_downloads` like RubyGems."""
    display = search.get("id") or name
    published = _iso((catalog_entry or {}).get("published"))
    if published is not None and published.year < 1970:
        published = None  # NuGet stamps unlisted versions with year 1900
    deprecation = (catalog_entry or {}).get("deprecation")
    note = None
    if isinstance(deprecation, dict):
        note = deprecation.get("message") or ", ".join(deprecation.get("reasons") or []) or None
    project_url = search.get("projectUrl") or ""
    repo_url = project_url if "github.com" in project_url.lower() else None
    return EcosystemPackage(
        ecosystem="nuget",
        name=display,
        registry_url=f"https://www.nuget.org/packages/{display}",
        latest_version=search.get("version"),
        latest_published_at=published,
        days_since_latest_publish=_days_since(published),
        versions_count=len(search.get("versions") or []) or None,
        total_downloads=search.get("totalDownloads"),
        license=_short_license(search.get("licenseExpression")),
        is_deprecated=bool(deprecation),
        deprecation_note=note,
        repository_url=repo_url,
        matches_repo=_repo_matches(repo_url, repo_full_name),
        # projectUrl doubles as the homepage when it isn't the repository link.
        homepage_url=_declared_url(project_url) if repo_url is None else None,
        keywords=[t for t in (search.get("tags") or []) if isinstance(t, str)],
        # NuGet distinguishes a plain dependency from a .NET tool and a project
        # template; only the non-default types are worth recording.
        declared_type=_nuget_package_type(search, catalog_entry),
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


def _split_keywords(value: Any) -> list[str]:
    """Normalize a registry's keywords field, which shows up as either a list
    of strings or (PyPI, some npm payloads) a single comma/space-delimited
    string."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    if isinstance(value, str):
        sep = "," if "," in value else None
        return [kw.strip() for kw in value.split(sep) if kw.strip()]
    return []


def _latest_stable_packagist(versions: dict[str, Any]) -> Optional[str]:
    stable = [v for v in versions if "dev" not in v.lower()]
    return (stable or list(versions))[0] if versions else None


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------


# Registry lookups are best-effort (a miss degrades one signal, never the
# scan), but registries do throw transient 5xx/connection errors under load —
# worth one short retry before giving up on the data point.
_FETCH_ATTEMPTS = 3
_FETCH_RETRY_DELAY_SECONDS = 1.0
# Rate-limit waits are longer than error retries on purpose: a 1-second retry
# inside the same rate window is a guaranteed second 429. Bounded twice: per
# call, and per client — ``_get`` serves every fetch in a scan, including every
# raw.githubusercontent manifest read, and a service throttling the whole scan
# could otherwise turn one repository into many minutes of sleeping. When the
# client's budget runs out, further 429s return immediately and flow into the
# ``failed``/carry-forward path, which exists for exactly this.
_RATE_LIMIT_RETRY_DELAY_SECONDS = 20.0
_RATE_LIMIT_MAX_WAIT_SECONDS = 60.0
_RATE_LIMIT_CLIENT_BUDGET_SECONDS = 120.0


def _get(client: httpx.Client, url: str) -> Optional[httpx.Response]:
    """GET with retries on network errors, 5xx, and 429; None when it stays down.

    429 is retryable by definition — the service is saying "later", not "no" —
    and it is honored, not just tolerated: the wait comes from ``Retry-After``
    when the service names one (capped, because a scan cannot block for an
    hour on a stats sidecar), and from a rate-limit default otherwise. Treating
    429 as a terminal answer is how locustio/locust's report came to carry a
    same-name stranger's 25 downloads/month while the real figure was 14M: the
    stats service throttled a bulk-scan wave, and the code filed "slow down"
    under "no data". Download figures must not depend on how busy the scanner
    happened to be that minute.
    """
    resp = None
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            resp = client.get(url)
        except httpx.HTTPError:
            resp = None
        if resp is not None and resp.status_code < 500 and resp.status_code != 429:
            return resp
        if attempt < _FETCH_ATTEMPTS:
            delay = _FETCH_RETRY_DELAY_SECONDS
            if resp is not None and resp.status_code == 429:
                delay = _RATE_LIMIT_RETRY_DELAY_SECONDS
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = min(float(retry_after), _RATE_LIMIT_MAX_WAIT_SECONDS)
                    except ValueError:
                        pass  # an HTTP-date Retry-After; the default stands
                # Waiting out rate limits is budgeted per client (= per scan):
                # patience with one throttled endpoint must not compound into
                # a scan that sleeps for minutes across dozens of fetches.
                budget = getattr(client, "_rate_limit_budget", _RATE_LIMIT_CLIENT_BUDGET_SECONDS)
                if delay > budget:
                    return resp
                client._rate_limit_budget = budget - delay
            time.sleep(delay)
    return resp


def _get_json(client: httpx.Client, url: str) -> Optional[Any]:
    resp = _get(client, url)
    if resp is None or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _get_text(client: httpx.Client, url: str) -> Optional[str]:
    resp = _get(client, url)
    return resp.text if resp is not None and resp.status_code == 200 else None


def _get_int(client: httpx.Client, url: str, *keys: str) -> Optional[int]:
    data = _get_json(client, url)
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, int) else None


def _get_stat(client: httpx.Client, url: str, *keys: str) -> tuple[Optional[int], str]:
    """A download statistic and how it was (not) obtained.

    States: ``published`` — the service answered with a figure; ``unpublished``
    — the service answered 404, it tracks nothing for this package (a brand-new
    package on pypistats, say); ``failed`` — the service errored, rate-limited
    through every retry, or answered malformed. The distinction is the whole
    point: ``unpublished`` is a fact about the package, ``failed`` is a fact
    about this scan, and only the second may be patched over with the previous
    scan's figure.
    """
    resp = _get(client, url)
    if resp is None or resp.status_code == 429 or resp.status_code >= 500:
        return None, "failed"
    if resp.status_code == 404:
        return None, "unpublished"
    if resp.status_code != 200:
        return None, "failed"
    try:
        data = resp.json()
    except ValueError:
        return None, "failed"
    for key in keys:
        if not isinstance(data, dict):
            return None, "failed"
        data = data.get(key)
    return (data, "published") if isinstance(data, int) else (None, "failed")


def _collect(
    contacts: Optional[list[ContactChannel]], extractor: Callable[[dict[str, Any]], list[ContactChannel]],
    payload: dict[str, Any],
) -> None:
    """Append a registry payload's contact channels, if the caller wants them.

    Contact extraction must never sink a package fetch: a malformed contact
    field is not a reason to lose the package's download counts and versions.
    """
    if contacts is None:
        return
    try:
        contacts.extend(extractor(payload))
    except Exception:
        pass


def fetch_pypi(client: httpx.Client, name: str, repo_full_name: str,
               contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://pypi.org/pypi/{name}/json")
    if not payload:
        return None
    _collect(contacts, contacts_from_pypi, payload)
    last_month, stats_state = _get_stat(
        client, f"https://pypistats.org/api/packages/{name.lower()}/recent", "data", "last_month"
    )
    pkg = map_pypi(name, payload, last_month, repo_full_name)
    if stats_state == "failed":
        pkg.downloads_state = "failed"
    return pkg


def fetch_npm(client: httpx.Client, name: str, repo_full_name: str,
              contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://registry.npmjs.org/{name}")
    if not payload:
        return None
    _collect(contacts, contacts_from_npm, payload)
    last_month, stats_state = _get_stat(
        client, f"https://api.npmjs.org/downloads/point/last-month/{name}", "downloads"
    )
    pkg = map_npm(name, payload, last_month, repo_full_name)
    if stats_state == "failed":
        pkg.downloads_state = "failed"
    return pkg


def fetch_packagist(client: httpx.Client, name: str, repo_full_name: str,
                    contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://packagist.org/packages/{name}.json")
    if not payload:
        return None
    _collect(contacts, contacts_from_packagist, payload)
    return map_packagist(name, payload, repo_full_name)


def fetch_crates(client: httpx.Client, name: str, repo_full_name: str,
                 contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://crates.io/api/v1/crates/{name}")
    if not payload:
        return None
    _collect(contacts, contacts_from_crates, payload)
    return map_crates(name, payload, repo_full_name)


def fetch_rubygems(client: httpx.Client, name: str, repo_full_name: str,
                   contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    gem = _get_json(client, f"https://rubygems.org/api/v1/gems/{name}.json")
    if not isinstance(gem, dict):
        return None
    _collect(contacts, contacts_from_rubygems, gem)
    versions = _get_json(client, f"https://rubygems.org/api/v1/versions/{name}.json")
    return map_rubygems(name, gem, versions if isinstance(versions, list) else [], repo_full_name)


def fetch_hex(client: httpx.Client, name: str, repo_full_name: str,
              contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    payload = _get_json(client, f"https://hex.pm/api/packages/{name}")
    if not isinstance(payload, dict):
        return None
    _collect(contacts, contacts_from_hex, payload)
    return map_hex(name, payload, repo_full_name)


def _go_escape(module: str) -> str:
    """Case-encode a module path for the proxy (uppercase -> '!' + lowercase)."""
    return "".join("!" + ch.lower() if ch.isupper() else ch for ch in module)


def fetch_go(client: httpx.Client, name: str, repo_full_name: str,
             contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    # The module proxy serves no author metadata — ``contacts`` is accepted to
    # keep every fetcher callable through FETCHERS, and stays unused.
    escaped = _go_escape(name)
    version_list = _get_text(client, f"https://proxy.golang.org/{escaped}/@v/list")
    versions = [v for v in (version_list or "").split() if v]
    if not versions:
        # Never tagged: the proxy would only serve pseudo-versions, so this
        # path is unpublished. When it's an untagged *submodule* of the
        # scanned repo (e.g. samples/go.mod), the published module is usually
        # the repo root path, whose tags live at the repo root — try that.
        root = f"github.com/{repo_full_name}"
        if name.lower() != root.lower() and name.lower().startswith(root.lower() + "/"):
            return fetch_go(client, root, repo_full_name, contacts)
        return None
    latest = _get_json(client, f"https://proxy.golang.org/{escaped}/@latest")
    if not isinstance(latest, dict):
        return None
    version = latest.get("Version")
    mod_text = (
        _get_text(client, f"https://proxy.golang.org/{escaped}/@v/{version}.mod")
        if version else None
    )
    return map_go(name, latest, versions, mod_text, repo_full_name)


def fetch_maven(client: httpx.Client, name: str, repo_full_name: str,
                contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    # Poms carry <developers><email>, but Maven Central's copy is frequently a
    # build artifact with the block stripped; left out until it earns its keep.
    if ":" not in name:
        return None
    group, artifact = name.split(":", 1)
    base = f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}"
    metadata_xml = _get_text(client, f"{base}/maven-metadata.xml")
    metadata = parse_maven_metadata(metadata_xml) if metadata_xml else None
    if not metadata:
        return None
    version = metadata["latest"]
    pom_xml = _get_text(client, f"{base}/{version}/{artifact}-{version}.pom")
    return map_maven(name, metadata, pom_xml, repo_full_name)


def _nuget_catalog_entry(
    client: httpx.Client, name: str, version: Optional[str]
) -> Optional[dict[str, Any]]:
    """Registration catalogEntry for one version (publish date, deprecation).
    Best-effort: only the newest few registration pages are searched."""
    if not version:
        return None
    index = _get_json(
        client, f"https://api.nuget.org/v3/registration5-gz-semver2/{name.lower()}/index.json"
    )
    wanted = version.split("+")[0].lower()
    for page in list(reversed((index or {}).get("items") or []))[:3]:
        items = page.get("items")
        if items is None and page.get("@id"):
            leaf = _get_json(client, page["@id"])
            items = (leaf or {}).get("items")
        for item in items or []:
            entry = item.get("catalogEntry") or {}
            if (entry.get("version") or "").split("+")[0].lower() == wanted:
                return entry
    return None


def fetch_nuget(client: httpx.Client, name: str, repo_full_name: str,
                contacts: Optional[list[ContactChannel]] = None) -> Optional[EcosystemPackage]:
    # The NuGet search index exposes no owner addresses, only owner names.
    data = _get_json(
        client,
        f"https://azuresearch-usnc.nuget.org/query?q=packageid:{name}&take=1&prerelease=false",
    )
    rows = (data or {}).get("data") or []
    if not rows:
        return None
    search = rows[0]
    entry = _nuget_catalog_entry(client, name, search.get("version"))
    return map_nuget(name, search, entry, repo_full_name)


FETCHERS: dict[
    str,
    Callable[[httpx.Client, str, str, Optional[list[ContactChannel]]], Optional[EcosystemPackage]],
] = {
    "pypi": fetch_pypi,
    "npm": fetch_npm,
    "packagist": fetch_packagist,
    "crates": fetch_crates,
    "rubygems": fetch_rubygems,
    "hex": fetch_hex,
    "go": fetch_go,
    "maven": fetch_maven,
    "nuget": fetch_nuget,
}


def _fetch_packages(
    client: httpx.Client, packages: list[tuple[str, str]], repo_full_name: str,
    warnings: list[str], contacts: Optional[list[ContactChannel]] = None,
    require_repo_match: bool = False,
) -> list[EcosystemPackage]:
    """Resolve (ecosystem, name) candidates against their registries.

    ``require_repo_match`` is for guessed names (see ``speculative_packages``):
    it drops any package whose registry entry does not point back at this
    repository, including one that names no repository at all.
    """
    results: list[EcosystemPackage] = []
    seen: set[tuple[str, str]] = set()
    for ecosystem, name in packages[:MAX_PACKAGES]:
        fetcher = FETCHERS.get(ecosystem)
        if not fetcher:
            continue
        # Per-package, so contacts from a registry entry we end up rejecting
        # never reach the report: a package pointing at a different repository
        # belongs to someone else, and its maintainers are not this repo's.
        found: list[ContactChannel] = []
        try:
            pkg = fetcher(client, name, repo_full_name, found)
        except Exception:
            pkg = None
        if pkg is None:
            if not require_repo_match:
                warnings.append(f"Could not fetch {ecosystem} package '{name}' from its registry")
            continue
        if require_repo_match and pkg.matches_repo is not True:
            # A guess that the registry does not tie back to this repository is
            # someone else's package; say nothing rather than claim it.
            continue
        # A fetcher may resolve to a canonical name (e.g. an untagged Go
        # submodule falling back to the repo-root module) — several manifests
        # can then converge on one package; keep it once.
        key = (pkg.ecosystem, pkg.name.lower())
        if key in seen:
            continue
        seen.add(key)
        # Registries whose figures ride the main payload never set a state
        # themselves; derive it from what the payload carried. Only the two
        # separate stats endpoints (pypistats, api.npmjs.org) can say "failed".
        if pkg.downloads_state is None:
            has_figures = pkg.monthly_downloads is not None or pkg.total_downloads is not None
            pkg.downloads_state = "published" if has_figures else "unpublished"
        if pkg.downloads_state == "failed" and pkg.matches_repo is not False:
            warnings.append(
                f"{ecosystem} download statistics for '{pkg.name}' unavailable this scan "
                "(stats endpoint failed after retries); adoption may be under-evidenced"
            )
        if pkg.matches_repo is False:
            warnings.append(
                f"{ecosystem} package '{name}' points at a different repository "
                f"({pkg.repository_url}); excluded from ecosystem scoring"
            )
        elif contacts is not None:
            contacts.extend(found)
        results.append(pkg)
    return results


def manifest_paths(tree_paths: list[str]) -> list[str]:
    """Root or one-level-deep manifest files for supported ecosystems.

    Sample, test and benchmark directories are skipped at this depth too: a
    `benchmarks/pom.xml` publishes nothing anyone installs, and in a repository
    at the package budget it displaces one that does.
    """
    out = []
    for path in tree_paths:
        if path.count("/") > 1 or _has_noise_segment(path):
            continue
        if ecosystem_for_manifest(path.rsplit("/", 1)[-1]):
            out.append(path)
    return out


def _has_noise_segment(path: str) -> bool:
    """Does any directory on the way to this file mark it as not-the-product?"""
    return any(seg.lower() in NOISE_PATH_SEGMENTS for seg in path.split("/")[:-1])


def nested_manifest_paths(
    tree_paths: list[str], limit: int = MAX_NESTED_MANIFESTS
) -> list[str]:
    """Two-level-deep manifests, for repositories whose surface has none.

    The conventional monorepo layouts — `src/Foo/Foo.csproj`,
    `libs/x/pyproject.toml`, `packages/x/package.json` — all sit at exactly
    this depth; in a production sample the winning manifest was at depth two in
    44 of 45 cases, so going deeper buys almost nothing and multiplies the
    fetch count. Sample and test directories are excluded, and the result is
    capped, because monorepos reach into the hundreds of manifests.
    """
    found = [
        path for path in tree_paths
        if path.count("/") == 2
        and ecosystem_for_manifest(path.rsplit("/", 1)[-1])
        and not _has_noise_segment(path)
    ]
    return sorted(found)[:limit]


def collect_ecosystem(
    owner: str, repo: str, branch: Optional[str], tree_paths: list[str],
    warnings: list[str],
) -> tuple[list[EcosystemPackage], list[Dependency], list[ContactChannel], dict[str, str]]:
    """Identify the repo's published packages, declared dependencies, contacts.

    Reads manifests already fetched from the repo (raw.githubusercontent): the
    declared package name feeds registry lookups (``EcosystemPackage``); the
    declared dependency list (``Dependency``) is reported as parsed, with no
    registry lookups, freshness checks, or vulnerability scanning.

    Registry payloads also carry maintainer contact channels, which come back
    as a third list rather than on ``EcosystemPackage`` — they are personal
    data, and keeping them off the published model keeps them out of the
    public report by construction.

    The fetched manifest texts come back as a fourth value. They are the only
    place the repository states what it *builds* (``bin``, ``OutputType``,
    Composer's ``type``), and re-fetching them elsewhere would double the raw
    fetches this function already paid for. See ``classify.py``.
    """
    paths = manifest_paths(tree_paths)
    nested = nested_manifest_paths(tree_paths)
    if not branch or (not paths and not nested):
        return [], [], [], {}
    repo_full_name = f"{owner}/{repo}"
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        texts: dict[str, str] = {}

        def read(manifest_paths_to_read: list[str]) -> dict[str, str]:
            fetched: dict[str, str] = {}
            for path in manifest_paths_to_read:
                url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
                text = _get_text(client, url)
                if text is not None:
                    fetched[path] = text
            texts.update(fetched)
            return fetched

        contacts: list[ContactChannel] = []
        surface = read(paths)
        identified = identify_packages(surface)
        packages = (
            _fetch_packages(client, identified, repo_full_name, warnings, contacts)
            if identified else []
        )

        def resolved() -> bool:
            """Did anything found so far actually name this repo's ecosystem?

            Mirrors the rule in ``ranked_ecosystems``: a package that does not
            exist, or whose registry entry names a different repository, is not
            this repository's. Testing membership of ``packages`` instead would
            let one manifest pointing at somebody else's package suppress the
            fallbacks and leave the repository with no ecosystem at all.
            """
            return any(p.exists and p.matches_repo is not False for p in packages)

        # Only when nothing on the surface resolved: the repository either has
        # no manifest at its root or publishes from a nested project directory.
        # Gated on the *result* rather than on "no manifest found" because a
        # manifest that names no resolvable package leaves us equally blind,
        # and unconditional descent would risk reordering the ecosystems of
        # repositories that are already correct for a ~1% yield.
        if not resolved() and nested:
            deep = read(nested)
            already = {(p.ecosystem, p.name.lower()) for p in packages}
            deep_identified = [
                (eco, name) for eco, name in identify_packages(deep)
                if (eco, name) not in identified and (eco, name.lower()) not in already
            ]
            if deep_identified:
                packages += _fetch_packages(
                    client, deep_identified, repo_full_name, warnings, contacts
                )

        # Last resort: manifests that declare no package name at all, guessed
        # from filename or repository name and kept only when the registry
        # points back here.
        if not resolved():
            guessed = speculative_packages(texts, repo_full_name)
            if guessed:
                packages += _fetch_packages(
                    client, guessed, repo_full_name, warnings, contacts,
                    require_repo_match=True,
                )

        # Names hidden behind `private: true`, verification-gated. NOT inside
        # the fallbacks above: a monorepo whose satellites resolved fine still
        # owes the registry a question about its flagship — that is exactly
        # apollo-client's shape, where gating this on "nothing resolved yet"
        # would skip the one package that matters.
        withheld = [
            (eco, name) for eco, name in private_manifest_candidates(texts)
            if (eco, name.lower()) not in {(p.ecosystem, p.name.lower()) for p in packages}
        ]
        if withheld:
            packages += _fetch_packages(
                client, withheld, repo_full_name, warnings, contacts,
                require_repo_match=True,
            )

        dependencies = collect_dependencies(texts)
        return packages, dependencies, dedupe_contacts(contacts), texts
