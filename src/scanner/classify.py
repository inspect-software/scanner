"""What the repository *builds*, and how the software is consumed.

The scanner knew a repository's health long before it knew what kind of thing
it was looking at. That gap is not cosmetic: a published library is expected
*not* to commit a lockfile, while the application next to it in the same
catalogue is expected to; a network service has an attack surface a parser
does not; a plugin inherits its host's trust model. Until this module existed,
one proxy stood in for all of it — "does the repo publish a package?" — which
reads every CLI on PyPI as a library and every monorepo service that happens to
ship a helper package as one too.

Three things make the answer usable rather than merely present:

**It is multi-label.** ripgrep is a crate *and* a binary; esbuild is an npm
package *and* an executable. Hybrids are the normal case, so a repository
carries every label its evidence supports and satisfies the expectations of all
of them. ``primary`` exists for display and comparison cohorts; nothing scores
off it.

**Evidence is tiered by how much it can be trusted.** A maintainer who writes
``<OutputType>Exe</OutputType>`` is not expressing an opinion — the build would
not work otherwise. A self-assigned GitHub topic is an opinion. The two are not
weighed alike, and no single weak signal alone can produce a label.

**Observation and interpretation are separate.** ``artifact_signals`` runs at
scan time and records canonical *tokens* — what the manifests declare and what
the file tree shows. ``classify`` maps tokens to labels and runs at scoring
time, from stored data only. So a mapping mistake is fixed by a rescore, not by
rescanning 30,000 repositories, and reports written before a token existed
simply carry less evidence rather than a wrong answer.

Silence is a valid outcome. No labels with confidence ``none`` means the
evidence did not answer the question — never that the repository is "neither".
"""

from __future__ import annotations

import configparser
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from typing import Iterable, Optional

from .ecosystems import NOISE_PATH_SEGMENTS, ecosystem_for_manifest, identify_packages
from .models import (
    ArtifactClassification,
    ArtifactSignals,
    Classification,
    ClassificationEvidence,
    EvidenceTier,
    ManifestDeclaration,
    RepoData,
)

# ---------------------------------------------------------------------------
# The vocabulary: a two-level tree
# ---------------------------------------------------------------------------

# Four top-level labels — the classification proper, and what the three flags
# derive from. They are not mutually exclusive: a repository that publishes a
# library and ships a binary answers yes twice and owes both sets of
# expectations.
#
# `library` has no subtypes on purpose. The vocabulary used to distinguish
# framework / sdk / api-client / middleware / driver, and those boundaries do
# not exist: axios is an api-client and a library, flask is a framework and a
# library, and no consumer treated the distinction as meaning anything. What
# a library *connects to* is the integration facet's job, not this one's.
#
# `notebook` belongs to no flag on purpose: it is read and executed by a
# person, not imported by other software, not deployed, and not installed into
# a host. It carries no obligation any of the flags would impose.
TOP_LABELS: frozenset[str] = frozenset(
    {"library", "application", "host-extension", "notebook"}
)

# Subtype -> parent. Subtypes exist only where the finer answer changes what a
# reader should expect: an executable's interface modality, or the host class
# an extension inherits its trust model from (which host *product* it extends
# is the integration facet's job).
SUBTYPE_PARENT: dict[str, str] = {
    "cli": "application",
    "tui": "application",
    "desktop": "application",
    "mobile": "application",
    "web-ui": "application",
    "network-service": "application",
    "chat-bot": "application",
    "mcp-server": "application",
    "plugin": "host-extension",
    "browser-extension": "host-extension",
    "editor-extension": "host-extension",
    "theme": "host-extension",
}

# Ranking used only to break ties for ``primary``: the reading with the largest
# attack surface wins, because that is the one an audit must not miss. A bare
# parent ranks after its subtypes — a specific answer beats a generic one.
SURFACE_ORDER: tuple[str, ...] = (
    "network-service",
    "web-ui",
    "chat-bot",
    "mcp-server",
    "desktop",
    "mobile",
    "cli",
    "tui",
    "application",
    "browser-extension",
    "plugin",
    "editor-extension",
    "theme",
    "host-extension",
    "notebook",
    "library",
)

# A label needs more than one weak observation. At 4.0, a single declared or
# distribution fact is decisive and one dependency fingerprint is enough, while
# a lone topic or a lone phrase in the description never is — two independent
# self-descriptions have to agree before the weakest tiers can carry a label on
# their own.
THRESHOLD = 4.0

# Default weight per tier. Individual rules override it where a token proves
# less than its tier usually does — a Cargo `[[bin]]` target says "this builds
# an executable", which is weaker than npm's `bin` saying "this installs a
# command onto your PATH".
TIER_WEIGHT: dict[str, float] = {
    "declared": 10.0,
    "distribution": 6.0,
    "structure": 3.0,
    "dependencies": 4.0,
    "tags": 2.0,
    "description": 2.0,
}


# ---------------------------------------------------------------------------
# Observation: manifest declarations (scan time, needs the manifest text)
# ---------------------------------------------------------------------------


def _json_or_none(text: str) -> Optional[dict]:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _toml_or_none(text: str) -> Optional[dict]:
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, TypeError):
        return None


def _xml_or_none(text: str) -> Optional[ET.Element]:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _package_json_tokens(text: str) -> list[str]:
    data = _json_or_none(text)
    if data is None:
        return []
    tokens = []
    if data.get("bin"):
        tokens.append("npm.bin")
    if any(data.get(key) for key in ("main", "exports", "module", "types", "typings")):
        tokens.append("npm.entry")
    if data.get("private") is True:
        tokens.append("npm.private")
    if data.get("workspaces"):
        tokens.append("npm.workspaces")
    if data.get("peerDependencies"):
        tokens.append("npm.peer_dependencies")
    if data.get("oclif"):
        tokens.append("npm.oclif")
    return tokens


# PyPI's classifier vocabulary is a tree, and most of it describes subject
# matter rather than use. These are the branches that say how the software is
# consumed; a classifier is recorded as the branch it belongs to, so
# "Environment :: X11 Applications :: Qt" and "… :: GTK" become one token.
_CANONICAL_CLASSIFIERS = (
    "Environment :: Console",
    "Environment :: Web Environment",
    "Environment :: X11 Applications",
    "Environment :: Win32 (MS Windows)",
    "Environment :: MacOS X",
    "Environment :: Plugins",
    "Topic :: Software Development :: Libraries",
)


def _classifier_tokens(classifiers: Iterable) -> list[str]:
    tokens = []
    for value in classifiers:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        for branch in _CANONICAL_CLASSIFIERS:
            if stripped.startswith(branch):
                tokens.append(f"pypi.classifier:{branch}")
                break
    return tokens


def _entry_point_tokens(groups: Iterable) -> list[str]:
    """Entry-point *group* names, which name the host a plugin plugs into.

    ``pytest11``, ``mkdocs.plugins``, ``flake8.extension`` — the group is the
    declaration, and it is reliable because the host reads exactly that key.
    """
    tokens = []
    for group in groups:
        if not isinstance(group, str):
            continue
        if group in ("console_scripts", "gui_scripts"):
            tokens.append(f"pypi.{group}")
        else:
            tokens.append(f"pypi.entry_point:{group}")
    return tokens


def _pyproject_tokens(text: str) -> list[str]:
    data = _toml_or_none(text)
    if data is None:
        return []
    tokens = []
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}

    if project.get("scripts") or poetry.get("scripts"):
        tokens.append("pypi.console_scripts")
    if project.get("gui-scripts"):
        tokens.append("pypi.gui_scripts")
    for source in (project.get("entry-points"), poetry.get("plugins")):
        if isinstance(source, dict):
            tokens += _entry_point_tokens(source.keys())
    for source in (project.get("classifiers"), poetry.get("classifiers")):
        if isinstance(source, list):
            tokens += _classifier_tokens(source)
    return tokens


_SETUP_CONSOLE_RE = re.compile(r"console_scripts")
_SETUP_GUI_RE = re.compile(r"gui_scripts")
_SETUP_CLASSIFIER_RE = re.compile(
    r"['\"](Environment :: [^'\"]+|Topic :: Software Development :: Libraries[^'\"]*)['\"]"
)


def _setup_tokens(text: str) -> list[str]:
    """setup.py / setup.cfg, read by pattern.

    Both formats put the same facts in too many shapes to parse properly, and
    the two that matter — a console script and a usage classifier — are
    unambiguous as substrings.
    """
    tokens = []
    if _SETUP_CONSOLE_RE.search(text):
        tokens.append("pypi.console_scripts")
    if _SETUP_GUI_RE.search(text):
        tokens.append("pypi.gui_scripts")
    tokens += _classifier_tokens(_SETUP_CLASSIFIER_RE.findall(text))
    return tokens


def _cargo_tokens(text: str) -> list[str]:
    data = _toml_or_none(text)
    if data is None:
        return []
    tokens = []
    package = data.get("package") if isinstance(data.get("package"), dict) else {}
    lib = data.get("lib") if isinstance(data.get("lib"), dict) else {}

    if data.get("bin"):
        tokens.append("cargo.bin")
    if lib:
        tokens.append("cargo.lib")
        crate_types = lib.get("crate-type") or lib.get("crate_type") or []
        if isinstance(crate_types, list):
            if any(t in ("cdylib", "staticlib") for t in crate_types):
                tokens.append("cargo.cdylib")
    if lib.get("proc-macro") is True or lib.get("proc_macro") is True:
        tokens.append("cargo.proc_macro")
    if package.get("publish") is False:
        tokens.append("cargo.publish_false")
    if data.get("workspace") is not None:
        tokens.append("cargo.workspace")
    categories = package.get("categories")
    if isinstance(categories, list):
        for category in categories:
            if isinstance(category, str):
                tokens.append(f"cargo.category:{category.strip().lower()}")
    return tokens


def _composer_tokens(text: str) -> list[str]:
    data = _json_or_none(text)
    if data is None:
        return []
    tokens = []
    declared = data.get("type")
    if isinstance(declared, str) and declared.strip():
        tokens.append(f"composer.type:{declared.strip().lower()}")
    if data.get("bin"):
        tokens.append("composer.bin")
    return tokens


_GEMSPEC_EXECUTABLES_RE = re.compile(r"\.(executables|bindir)\s*=")
_GEMSPEC_RAILTIES_RE = re.compile(r"add_(runtime_)?dependency\s*\(?\s*['\"]railties['\"]")


def _gemspec_tokens(text: str) -> list[str]:
    tokens = ["gem.gemspec"]
    if _GEMSPEC_EXECUTABLES_RE.search(text):
        tokens.append("gem.executables")
    if _GEMSPEC_RAILTIES_RE.search(text):
        tokens.append("gem.rails_engine")
    return tokens


def _pom_tokens(text: str) -> list[str]:
    root = _xml_or_none(text)
    if root is None:
        return []
    tokens = []
    packaging = root.find("{*}packaging")
    if packaging is not None and (packaging.text or "").strip():
        tokens.append(f"maven.packaging:{packaging.text.strip().lower()}")
    if "spring-boot-maven-plugin" in text:
        tokens.append("maven.spring_boot")
    if root.find(".//{*}mainClass") is not None:
        tokens.append("maven.main_class")
    return tokens


def _csproj_tokens(text: str) -> list[str]:
    root = _xml_or_none(text)
    if root is None:
        return []
    tokens = []
    sdk = (root.get("Sdk") or "").lower()
    if "sdk.web" in sdk:
        tokens.append("nuget.sdk:web")
    elif "sdk.worker" in sdk:
        tokens.append("nuget.sdk:worker")

    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        value = (node.text or "").strip()
        if tag == "OutputType" and value:
            tokens.append(f"nuget.output_type:{value.lower()}")
        elif tag == "PackAsTool" and value.lower() == "true":
            tokens.append("nuget.pack_as_tool")
        elif tag == "IsPackable" and value.lower() == "false":
            tokens.append("nuget.not_packable")
        elif tag == "TargetFramework" and "-" in value:
            tokens.append(f"nuget.platform_tfm:{value.split('-', 1)[1].lower()}")
    return tokens


_MIX_MOD_RE = re.compile(r"\bmod:\s*\{")
_MIX_ESCRIPT_RE = re.compile(r"\bescript:\s")
_MIX_RELEASES_RE = re.compile(r"\breleases:\s")


def _mix_tokens(text: str) -> list[str]:
    tokens = []
    if _MIX_MOD_RE.search(text):
        tokens.append("mix.mod")
    if _MIX_ESCRIPT_RE.search(text):
        tokens.append("mix.escript")
    if _MIX_RELEASES_RE.search(text):
        tokens.append("mix.releases")
    return tokens


_APP_SRC_MOD_RE = re.compile(r"\{\s*mod\s*,")


def _app_src_tokens(text: str) -> list[str]:
    return ["otp.mod"] if _APP_SRC_MOD_RE.search(text) else []


def declaration_tokens(path: str, text: str) -> list[str]:
    """Canonical tokens for one manifest, or [] when it declares nothing useful.

    Unrecognized manifests are not an error: go.mod declares nothing about the
    artifact (Go says it in the file tree instead), and a Gemfile describes a
    dependency set rather than a product.
    """
    filename = path.rsplit("/", 1)[-1]
    lower = filename.lower()
    if lower == "package.json":
        return _package_json_tokens(text)
    if lower == "pyproject.toml":
        return _pyproject_tokens(text)
    if lower in ("setup.py", "setup.cfg"):
        return _setup_tokens(text)
    if lower == "cargo.toml":
        return _cargo_tokens(text)
    if lower == "composer.json":
        return _composer_tokens(text)
    if lower.endswith(".gemspec"):
        return _gemspec_tokens(text)
    if lower == "pom.xml":
        return _pom_tokens(text)
    if lower.endswith(".csproj"):
        return _csproj_tokens(text)
    if lower == "mix.exs":
        return _mix_tokens(text)
    if lower.endswith(".app.src"):
        return _app_src_tokens(text)
    return []


# Linter/formatter configuration embedded in ecosystem manifests. The tree
# scan (collect.LINTER_CONFIG_NAMES) can only see standalone config files,
# but the dominant convention today embeds the config in the manifest the
# project already has: [tool.ruff] in pyproject.toml, eslintConfig in
# package.json, [lints] in Cargo.toml. A project doing that is not less
# linted for it.
_PYPROJECT_LINTER_TOOLS = (
    "ruff", "flake8", "pylint", "black", "isort",
    "yapf", "autopep8", "pycodestyle",
)
_SETUP_CFG_LINTER_SECTIONS = ("flake8", "pycodestyle", "pylint", "isort", "yapf")
_PACKAGE_JSON_LINTER_KEYS = ("eslintConfig", "prettier", "xo", "standard")


def embedded_linter_configs(manifest_texts: dict[str, str]) -> list[str]:
    """Linter/formatter config declared inside manifests, as display entries.

    One entry per manifest that declares any, naming the section(s) found —
    e.g. ``pyproject.toml ([tool.ruff], [tool.black])`` — so the report shows
    the evidence the same way it shows standalone config filenames. Manifests
    that fail to parse contribute nothing.
    """
    found: list[str] = []
    for path in sorted(manifest_texts):
        filename = path.rsplit("/", 1)[-1].lower()
        text = manifest_texts[path]
        sections: list[str] = []
        if filename == "pyproject.toml":
            data = _toml_or_none(text)
            tool = data.get("tool") if data else None
            if isinstance(tool, dict):
                sections = [
                    f"[tool.{name}]" for name in _PYPROJECT_LINTER_TOOLS
                    if name in tool
                ]
        elif filename == "setup.cfg":
            parser = configparser.ConfigParser()
            try:
                parser.read_string(text)
            except configparser.Error:
                continue
            sections = [
                f"[{name}]" for name in _SETUP_CFG_LINTER_SECTIONS
                if parser.has_section(name)
            ]
        elif filename == "package.json":
            data = _json_or_none(text)
            if data:
                sections = [
                    f'"{key}"' for key in _PACKAGE_JSON_LINTER_KEYS
                    if data.get(key)
                ]
        elif filename == "cargo.toml":
            data = _toml_or_none(text)
            if data:
                if data.get("lints"):
                    sections.append("[lints]")
                workspace = data.get("workspace")
                if isinstance(workspace, dict) and workspace.get("lints"):
                    sections.append("[workspace.lints]")
        if sections:
            found.append(f"{path} ({', '.join(sections)})")
    return found


# ---------------------------------------------------------------------------
# Observation: file-tree structure (scan time, needs the tree)
# ---------------------------------------------------------------------------

_ROOT_FILE_TOKENS: dict[str, str] = {
    "procfile": "tree.procfile",
    "config.ru": "tree.config_ru",
    ".vscodeignore": "tree.vscode_extension",
    ".goreleaser.yml": "tree.goreleaser",
    ".goreleaser.yaml": "tree.goreleaser",
    "goreleaser.yml": "tree.goreleaser",
    "serverless.yml": "tree.serverless",
    "serverless.yaml": "tree.serverless",
    "vercel.json": "tree.serverless",
    "netlify.toml": "tree.serverless",
    "fly.toml": "tree.serverless",
    "render.yaml": "tree.serverless",
    "index.html": "tree.static_site",
}

_BASENAME_TOKENS: dict[str, str] = {
    "tauri.conf.json": "tree.tauri",
    "electron-builder.yml": "tree.electron",
    "electron-builder.yaml": "tree.electron",
    "electron-builder.json": "tree.electron",
    "forge.config.js": "tree.electron",
    "forge.config.cjs": "tree.electron",
    "snapcraft.yaml": "tree.snapcraft",
    "androidmanifest.xml": "tree.android_manifest",
    "info.plist": "tree.apple_bundle",
    "chart.yaml": "tree.helm",
}

_EXACT_PATH_TOKENS: dict[str, str] = {
    "src/main.rs": "tree.cargo_main",
    "src/lib.rs": "tree.cargo_lib",
    "main.go": "tree.go_main",
    "config/routes.rb": "tree.rails_app",
    "public/index.html": "tree.static_site",
}

_K8S_DIRS = frozenset({"k8s", "kubernetes", "helm", "charts"})
_MIGRATION_DIRS = frozenset({"migrations", "migrate", "alembic"})
_EXTENSION_COMPANIONS = frozenset(
    {"popup.html", "background.js", "content.js", "content_script.js", "service_worker.js"}
)


def _is_noise(lower_path: str) -> bool:
    return any(seg in NOISE_PATH_SEGMENTS for seg in lower_path.split("/")[:-1])


def structure_tokens(tree_paths: Iterable[str]) -> list[str]:
    """Repository-level tokens for what the file tree shows it builds.

    Paths under sample, test and vendor directories are ignored for the same
    reason the manifest scan ignores them: an example service in ``examples/``
    is not what the repository is.
    """
    found: set[str] = set()
    root_files: set[str] = set()
    companions = False
    root_go_sources = False
    root_go_main = False

    for path in tree_paths:
        lower = path.lower()
        if _is_noise(lower):
            continue
        parts = lower.split("/")
        filename = parts[-1]
        depth = len(parts) - 1

        if depth == 0:
            root_files.add(filename)
            if filename in _ROOT_FILE_TOKENS:
                found.add(_ROOT_FILE_TOKENS[filename])
        if filename in _BASENAME_TOKENS:
            found.add(_BASENAME_TOKENS[filename])
        if lower in _EXACT_PATH_TOKENS:
            found.add(_EXACT_PATH_TOKENS[lower])

        if filename == "dockerfile" or filename.startswith("dockerfile."):
            found.add("tree.dockerfile")
        if filename.startswith(("docker-compose", "compose.")) and filename.endswith(
            (".yml", ".yaml")
        ):
            found.add("tree.compose")
        if filename.endswith(".desktop"):
            found.add("tree.desktop_entry")
        if filename.endswith(".ipynb") and depth <= 1:
            found.add("tree.notebook")
        if filename in _EXTENSION_COMPANIONS or parts[0] == "_locales":
            companions = True
        if parts[0] in _K8S_DIRS and filename.endswith((".yml", ".yaml")):
            found.add("tree.k8s")
        if any(part in _MIGRATION_DIRS for part in parts[:-1]):
            found.add("tree.migrations")
        # Go states what it builds in the file tree — the language has no
        # manifest field for it. `cmd/<name>/` is the documented layout for
        # commands, so any source directly inside one is a binary target, not
        # only a file literally named main.go (go-critic's second command lives
        # in cmd/go-critic-analysis/go-critic-analysis.go).
        if filename.endswith(".go") and not filename.endswith("_test.go"):
            if parts[0] == "cmd" and depth == 2:
                found.add("tree.go_main")
            elif depth == 0:
                # Root sources are one package: if main.go sits among them the
                # package is `main` and none of its siblings can be imported.
                # Decided after the loop, when both facts are known.
                root_go_sources = True
                if filename == "main.go":
                    root_go_main = True
            elif parts[0] != "cmd" and "internal" not in parts[:-1] and parts[0] != "testdata":
                # A package outside cmd/ that the `internal` rule does not
                # seal off. The compiler enforces that rule, which is what
                # makes its absence meaningful: this code *can* be imported.
                found.add("tree.go_importable")
        # Cargo's binary directory, and nested crates in a workspace.
        if lower.endswith("/src/main.rs") or (parts[0] == "src" and parts[1:2] == ["bin"]):
            found.add("tree.cargo_main")
        if lower.endswith("/src/lib.rs"):
            found.add("tree.cargo_lib")

    if "manifest.json" in root_files and companions:
        found.add("tree.browser_extension")
    if root_go_sources and not root_go_main:
        found.add("tree.go_importable")
    return sorted(found)


# Manifests whose *filename* is the artifact name by convention. Sinatra's
# gemspec declares its name positionally (`Gem::Specification.new 'sinatra', …`)
# where no parser looks, and the file is called sinatra.gemspec for exactly the
# reason that makes this reliable.
_NAME_BEARING_SUFFIXES = (".gemspec", ".csproj", ".app.src")


def _declared_name(path: str, text: str) -> Optional[str]:
    identified = identify_packages({path: text})
    if identified:
        return identified[0][1]
    filename = path.rsplit("/", 1)[-1]
    for suffix in _NAME_BEARING_SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)] or None
    return None


def artifact_signals(
    manifest_texts: dict[str, str], tree_paths: Iterable[str]
) -> ArtifactSignals:
    """Everything observable about what this repository builds.

    ``manifest_texts`` are the manifests the ecosystem scan already fetched, so
    this costs no additional network request.
    """
    declarations = []
    for path in sorted(manifest_texts):
        text = manifest_texts[path]
        tokens = declaration_tokens(path, text)
        if not tokens:
            continue
        ecosystem = ecosystem_for_manifest(path.rsplit("/", 1)[-1]) or "unknown"
        declarations.append(
            ManifestDeclaration(
                path=path,
                ecosystem=ecosystem,
                name=_declared_name(path, text),
                tokens=tokens,
            )
        )
    return ArtifactSignals(
        collected=True,
        declarations=declarations,
        structure=structure_tokens(tree_paths),
    )


# ---------------------------------------------------------------------------
# Interpretation: token -> label (pure, runs at scoring time)
# ---------------------------------------------------------------------------

# (label, weight). Negative weights rule a label out: a Cargo package with a
# binary target and `publish = false` is not something anyone can depend on.
Rule = tuple[str, float]

DECLARED_RULES: dict[str, tuple[Rule, ...]] = {
    # npm
    "npm.bin": (("cli", 10.0),),
    "npm.entry": (("library", 8.0),),
    "npm.private": (("library", -6.0),),
    "npm.peer_dependencies": (("library", 3.0),),
    "npm.oclif": (("cli", 10.0),),
    # PyPI
    "pypi.console_scripts": (("cli", 10.0),),
    "pypi.gui_scripts": (("desktop", 10.0),),
    "pypi.classifier:Environment :: Console": (("cli", 6.0),),
    # "Environment :: Web Environment" is recorded but unmapped: it failed the
    # reliability gate on real data, where `requests` — the reference HTTP
    # *library* — declares it. HTTP clients read it as "web-related".
    "pypi.classifier:Environment :: X11 Applications": (("desktop", 6.0),),
    "pypi.classifier:Environment :: Win32 (MS Windows)": (("desktop", 6.0),),
    "pypi.classifier:Environment :: MacOS X": (("desktop", 6.0),),
    "pypi.classifier:Topic :: Software Development :: Libraries": (("library", 6.0),),
    "pypi.classifier:Environment :: Plugins": (("plugin", 6.0),),
    # Cargo. `[[bin]]` proves an executable, not a command-line interface —
    # a web server declares one too — so it is deliberately weaker than npm's
    # `bin`, which installs a command onto the user's PATH.
    "cargo.bin": (("application", 5.0),),
    "cargo.lib": (("library", 10.0),),
    "cargo.cdylib": (("library", 6.0),),
    "cargo.proc_macro": (("library", 10.0),),
    "cargo.publish_false": (("library", -6.0),),
    "cargo.category:command-line-utilities": (("cli", 6.0),),
    "cargo.category:web-programming::http-server": (("network-service", 6.0),),
    "cargo.category:api-bindings": (("library", 6.0),),
    "cargo.category:game-engines": (("library", 6.0),),
    "cargo.category:gui": (("desktop", 6.0),),
    "cargo.category:development-tools::procedural-macro-helpers": (("library", 6.0),),
    # Composer publishes the type outright — the single cleanest declaration
    # in any ecosystem.
    "composer.type:library": (("library", 10.0),),
    "composer.type:project": (("library", -8.0),),
    "composer.type:wordpress-plugin": (("plugin", 10.0),),
    "composer.type:drupal-module": (("plugin", 10.0),),
    "composer.type:magento2-module": (("plugin", 10.0),),
    "composer.type:typo3-cms-extension": (("plugin", 10.0),),
    "composer.type:composer-plugin": (("plugin", 10.0),),
    "composer.type:symfony-bundle": (("plugin", 8.0), ("library", 4.0)),
    "composer.bin": (("cli", 10.0),),
    # RubyGems
    "gem.gemspec": (("library", 8.0),),
    "gem.executables": (("cli", 10.0),),
    "gem.rails_engine": (("plugin", 8.0),),
    # Maven
    "maven.packaging:jar": (("library", 6.0),),
    "maven.packaging:war": (("network-service", 10.0), ("library", -8.0)),
    "maven.packaging:ear": (("network-service", 8.0), ("library", -8.0)),
    "maven.packaging:maven-plugin": (("plugin", 10.0),),
    "maven.spring_boot": (("network-service", 8.0),),
    "maven.main_class": (("application", 4.0),),
    # NuGet / MSBuild
    "nuget.output_type:exe": (("application", 5.0),),
    "nuget.output_type:winexe": (("desktop", 8.0),),
    "nuget.output_type:library": (("library", 8.0),),
    "nuget.sdk:web": (("network-service", 10.0), ("library", -8.0)),
    "nuget.sdk:worker": (("network-service", 8.0),),
    "nuget.pack_as_tool": (("cli", 10.0), ("library", -8.0)),
    "nuget.not_packable": (("library", -6.0),),
    "nuget.platform_tfm:windows": (("desktop", 6.0),),
    "nuget.platform_tfm:android": (("mobile", 6.0),),
    "nuget.platform_tfm:ios": (("mobile", 6.0),),
    # BEAM. `mod:` is recorded but deliberately unmapped: libraries that start
    # a supervisor (connection pools, caches) declare it exactly as
    # applications do, so it fails the reliability gate.
    "mix.escript": (("cli", 10.0),),
    "mix.releases": (("network-service", 8.0),),
}

# Entry-point groups other than console/gui scripts name the host that reads
# them, which is what makes them a plugin declaration rather than a guess.
ENTRY_POINT_PREFIX = "pypi.entry_point:"
ENTRY_POINT_RULE: tuple[Rule, ...] = (("plugin", 8.0),)

STRUCTURE_RULES: dict[str, tuple[Rule, ...]] = {
    # Go's command layout is the language's own artifact declaration, so it
    # carries more than ordinary structure — but it proves an *executable*,
    # not a command-line interface (Grafana's servers live in cmd/ too), so it
    # matches Cargo's binary target rather than npm's `bin`.
    "tree.go_main": (("application", 5.0),),
    "tree.go_importable": (("library", 3.0),),
    "tree.cargo_main": (("application", 3.0),),
    "tree.cargo_lib": (("library", 3.0),),
    "tree.compose": (("network-service", 3.0),),
    "tree.k8s": (("network-service", 4.0),),
    "tree.helm": (("network-service", 4.0),),
    "tree.procfile": (("network-service", 4.0),),
    "tree.serverless": (("network-service", 4.0),),
    "tree.config_ru": (("network-service", 4.0),),
    "tree.rails_app": (("network-service", 4.0),),
    "tree.goreleaser": (("application", 4.0),),
    "tree.tauri": (("desktop", 8.0),),
    "tree.electron": (("desktop", 8.0),),
    "tree.snapcraft": (("desktop", 3.0),),
    "tree.desktop_entry": (("desktop", 4.0),),
    "tree.android_manifest": (("mobile", 6.0),),
    "tree.browser_extension": (("browser-extension", 8.0),),
    "tree.vscode_extension": (("editor-extension", 8.0),),
    "tree.notebook": (("notebook", 3.0),),
    "tree.static_site": (("web-ui", 3.0),),
    # Recorded but unmapped: a Dockerfile is CI tooling as often as it is the
    # product, `Info.plist` appears in test bundles, and a `migrations/`
    # directory belongs to any library that owns a schema — tauri has one.
}

# Registry-declared types. Fewer registries publish one than should, but where
# they do it is authoritative.
REGISTRY_TYPE_RULES: dict[str, tuple[Rule, ...]] = {
    "library": (("library", 6.0),),
    "project": (("library", -6.0),),
    "wordpress-plugin": (("plugin", 6.0),),
    "drupal-module": (("plugin", 6.0),),
    "magento2-module": (("plugin", 6.0),),
    "composer-plugin": (("plugin", 6.0),),
    "symfony-bundle": (("plugin", 6.0),),
    "dotnettool": (("cli", 6.0), ("library", -6.0)),
    "template": (("plugin", 4.0),),
    "war": (("network-service", 6.0), ("library", -6.0)),
    "maven-plugin": (("plugin", 6.0),),
    "jar": (("library", 4.0),),
}

REGISTRY_CATEGORY_RULES: dict[str, tuple[Rule, ...]] = {
    "command-line-utilities": (("cli", 6.0),),
    "web-programming::http-server": (("network-service", 6.0),),
    "api-bindings": (("library", 6.0),),
    "game-engines": (("library", 6.0),),
    "gui": (("desktop", 6.0),),
}

# Direct dependencies that only appear in one kind of software. Each entry has
# to survive the same question: would essentially every project depending on
# this ship the label? "requests" fails it; "axum" does not.
DEPENDENCY_RULES: dict[str, str] = {}


def _add_dependencies(label: str, names: Iterable[str]) -> None:
    for name in names:
        DEPENDENCY_RULES[name.lower()] = label


_add_dependencies(
    "cli",
    (
        "click", "typer", "docopt", "fire", "cleo", "rich-click", "argcomplete",
        "commander", "yargs", "@oclif/core", "oclif", "cac", "meow", "commandline",
        "clap", "structopt", "argh",
        "github.com/spf13/cobra", "github.com/urfave/cli", "github.com/urfave/cli/v2",
        "github.com/alecthomas/kingpin",
        "symfony/console", "thor", "slop", "optimist",
        "info.picocli:picocli", "system.commandline",
    ),
)
_add_dependencies(
    "tui",
    (
        "textual", "urwid", "blessed", "ink", "ratatui", "tui", "cursive", "crossterm",
        "github.com/charmbracelet/bubbletea", "github.com/rivo/tview",
        "github.com/gdamore/tcell", "github.com/gdamore/tcell/v2",
    ),
)
# Web *frameworks* only. Servers that merely run someone else's application —
# puma, gunicorn, uvicorn, plug_cowboy — are excluded: they turn up in the
# development extras of libraries that have to test against a real server, and
# in a Gemfile they cost sinatra a false "network-service" of its own.
_add_dependencies(
    "network-service",
    (
        "fastapi", "flask", "django", "starlette", "sanic", "tornado", "litestar",
        "express", "fastify", "koa", "@nestjs/core", "@hapi/hapi",
        "actix-web", "axum", "rocket", "warp", "tide", "poem", "salvo",
        "github.com/gin-gonic/gin", "github.com/labstack/echo",
        "github.com/labstack/echo/v4", "github.com/gofiber/fiber",
        "github.com/gofiber/fiber/v2", "github.com/go-chi/chi",
        "phoenix",
        "rails", "sinatra", "roda", "hanami",
        "laravel/framework", "symfony/framework-bundle", "slim/slim",
        "org.springframework.boot:spring-boot-starter-web",
        "io.quarkus:quarkus-resteasy", "microsoft.aspnetcore.app",
    ),
)
_add_dependencies(
    "web-ui",
    ("react-dom", "vue", "svelte", "@angular/core", "next", "nuxt", "solid-js", "preact"),
)
_add_dependencies(
    "desktop",
    (
        "electron", "@tauri-apps/api", "tauri", "wails", "pywebview", "pyqt5", "pyqt6",
        "pyside6", "kivy", "wxpython", "iced", "egui", "eframe", "gtk4", "avaloniaui",
        "github.com/wailsapp/wails", "github.com/wailsapp/wails/v2",
    ),
)
_add_dependencies(
    "chat-bot",
    (
        "discord.py", "discord.js", "nextcord", "hikari", "telegraf",
        "python-telegram-bot", "aiogram", "pytelegrambotapi", "slack-bolt",
        "slack_bolt", "@slack/bolt", "serenity", "teloxide",
        "github.com/bwmarrin/discordgo",
    ),
)
_add_dependencies(
    "mcp-server",
    ("mcp", "fastmcp", "@modelcontextprotocol/sdk", "modelcontextprotocol", "rmcp"),
)

# Topics and registry keywords. Self-assigned, so weak by construction and
# never sufficient alone — but they are the only evidence a repository with no
# manifest at all offers.
TAG_RULES: dict[str, str] = {}

_TAG_GROUPS: dict[str, tuple[str, ...]] = {
    "cli": ("cli", "cli-tool", "cli-app", "command-line", "command-line-tool", "commandline", "coreutils"),
    "tui": ("tui", "terminal-ui"),
    "desktop": ("desktop", "desktop-app", "desktop-application", "electron-app"),
    "mobile": ("mobile-app", "android-app", "ios-app"),
    "web-ui": ("dashboard", "admin-panel", "webapp", "web-app", "spa"),
    "chat-bot": ("chatbot", "discord-bot", "telegram-bot", "slack-bot", "whatsapp-bot"),
    "notebook": ("jupyter-notebook", "notebook"),
    # Everything the old framework/sdk/api-client/middleware/driver labels
    # answered to. The bare `driver` tag maps to nothing on purpose: it covers
    # kernel and device drivers — which install into a host — as readily as
    # database clients, so it fails the reliability gate in both directions.
    "library": (
        "library", "python-library", "go-library", "rust-library", "java-library",
        "js-library", "framework", "web-framework", "agent-framework", "game-engine",
        "sdk", "api-bindings", "bindings", "api-client", "http-client", "rest-client",
        "middleware", "database-driver", "connector",
    ),
    "network-service": ("http-server", "api-gateway", "reverse-proxy", "daemon", "microservice", "self-hosted", "webserver"),
    "plugin": ("plugin", "wordpress-plugin", "obsidian-plugin", "pytest-plugin", "gradle-plugin", "eslint-plugin", "terraform-provider", "magento2-module"),
    "browser-extension": ("browser-extension", "chrome-extension", "firefox-addon"),
    "editor-extension": ("vscode-extension", "language-server", "lsp", "language-server-protocol"),
    "theme": ("theme", "jekyll-theme", "hugo-theme"),
    "mcp-server": ("mcp-server", "mcp-tools"),
}
for _label, _tags in _TAG_GROUPS.items():
    for _tag in _tags:
        TAG_RULES[_tag] = _label

DESCRIPTION_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(cli|command[- ]line)\b", re.I), "cli"),
    (re.compile(r"\b(tui|terminal ui)\b", re.I), "tui"),
    (re.compile(r"\blibrary\b", re.I), "library"),
    (re.compile(r"\bframework\b", re.I), "library"),
    (re.compile(r"\bsdk\b", re.I), "library"),
    (re.compile(r"\bplugin\b", re.I), "plugin"),
    (re.compile(r"\b(browser |chrome |firefox )extension\b", re.I), "browser-extension"),
    (re.compile(r"\bmcp server\b", re.I), "mcp-server"),
    (re.compile(r"\b(self-hosted|daemon|microservice)\b", re.I), "network-service"),
    (re.compile(r"\b(dashboard|web (ui|interface))\b", re.I), "web-ui"),
    (re.compile(r"\bdesktop (app|application)\b", re.I), "desktop"),
    (re.compile(r"\b(discord|telegram|slack) bot\b", re.I), "chat-bot"),
)


# ---------------------------------------------------------------------------
# Interpretation: the classifier itself
# ---------------------------------------------------------------------------


class _Ledger:
    """Collected evidence, deduplicated by what was observed.

    Deduplication is the difference between a correct answer and a monorepo
    scoring "library" twenty times because it holds twenty package.json files.
    The same observation repeated is still one observation.
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str, str], ClassificationEvidence] = {}

    def add(self, label: str, tier: EvidenceTier, weight: float, source: str) -> None:
        key = (label, tier, source)
        existing = self._seen.get(key)
        if existing is None or abs(weight) > abs(existing.weight):
            self._seen[key] = ClassificationEvidence(
                label=label, tier=tier, weight=weight, source=source
            )

    def apply(
        self, rules: tuple[Rule, ...], tier: EvidenceTier, source: str
    ) -> None:
        for label, weight in rules:
            self.add(label, tier, weight, source)

    def evidence(self) -> list[ClassificationEvidence]:
        return sorted(
            self._seen.values(), key=lambda e: (-abs(e.weight), e.label, e.source)
        )

    def scores(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for item in self._seen.values():
            totals[item.label] = totals.get(item.label, 0.0) + item.weight
        return totals

    def remove(self, sources: set[str]) -> None:
        """Withdraw every entry whose source is in ``sources``.

        For conditional rules: an observation collected under an assumption the
        rest of the evidence later contradicts is removed outright rather than
        outweighed, so it neither scores nor appears as evidence.
        """
        for key in [k for k in self._seen if k[2] in sources]:
            del self._seen[key]


def _rules_for_token(token: str) -> tuple[Rule, ...]:
    if token in DECLARED_RULES:
        return DECLARED_RULES[token]
    if token.startswith(ENTRY_POINT_PREFIX):
        return ENTRY_POINT_RULE
    return ()


def _resolve(scores: dict[str, float]) -> tuple[list[str], list[str]]:
    """(labels, top) supported by the scores, under the tree's rules.

    A subtype crossing the threshold carries its parent with it. A parent can
    also cross on its own evidence — Go's ``cmd/`` proves an executable without
    saying what kind — in which case the bare parent is the answer. ``labels``
    holds the most specific answer per branch: subtypes where any exist, the
    bare parent otherwise, and top-level leaves (library, notebook) as
    themselves.
    """
    subtypes = [
        label
        for label, score in scores.items()
        if label in SUBTYPE_PARENT and score >= THRESHOLD
    ]
    tops = {SUBTYPE_PARENT[label] for label in subtypes}
    tops.update(
        label
        for label, score in scores.items()
        if label in TOP_LABELS and score >= THRESHOLD
    )

    def rank(label: str) -> tuple[float, int]:
        # A subtype is ranked by the strength of its whole branch: evidence
        # that proved "an executable" argues for the branch's specific reading
        # too, it just could not name it. Without this, go-critic's `cmd/`
        # directory (application, 5.0) would count for nothing when ranking
        # `cli` against `library`.
        strength = scores.get(label, 0.0) + scores.get(SUBTYPE_PARENT.get(label, ""), 0.0)
        return (-strength, SURFACE_ORDER.index(label))

    parents_with_subtypes = {SUBTYPE_PARENT[label] for label in subtypes}
    labels = subtypes + [top for top in tops if top not in parents_with_subtypes]
    return sorted(labels, key=rank), sorted(tops, key=rank)


# An unpublished package describes the repository's own tooling, not its
# product: the command it installs is a build script, and the entry point it
# exports is imported by nothing. WooCommerce — a WordPress plugin — reads as a
# command-line tool without this, on the strength of a private monorepo
# package.json wiring up oclif.
_PRIVATE_SUPPRESSES = {"npm.bin", "npm.entry", "npm.oclif", "npm.peer_dependencies"}


def _effective_tokens(tokens: list[str]) -> list[str]:
    if "npm.private" in tokens:
        return [t for t in tokens if t not in _PRIVATE_SUPPRESSES]
    return tokens


# A manifest at the repository root describes the product; one several
# directories down describes a part of it. Both are true, but when they
# disagree the root is what the repository *is* — without this, WooCommerce
# reads as a command-line tool because a build utility in `tools/` is published
# as one.
NESTED_MANIFEST_DISCOUNT = 0.6


def _collect_declared(data: RepoData, ledger: _Ledger) -> list[ArtifactClassification]:
    artifacts = []
    for declaration in data.artifacts.declarations:
        per_artifact = _Ledger()
        discount = 1.0 if "/" not in declaration.path else NESTED_MANIFEST_DISCOUNT
        for token in _effective_tokens(declaration.tokens):
            rules = _rules_for_token(token)
            if not rules:
                continue
            # The repository-level view is weighted by where the manifest sits;
            # the per-artifact view answers a question about the manifest
            # itself, where its depth is irrelevant.
            ledger.apply(
                tuple((label, round(weight * discount, 1)) for label, weight in rules),
                "declared",
                token,
            )
            per_artifact.apply(rules, "declared", token)
        artifacts.append(
            ArtifactClassification(
                path=declaration.path,
                ecosystem=declaration.ecosystem,
                labels=_resolve(per_artifact.scores())[0],
            )
        )
    return artifacts


# The Go module proxy is not a registry in the sense the distribution tier
# assumes. Publishing to npm, PyPI or crates.io is an intentional act; the
# proxy indexes any repository with a go.mod the moment anyone requests it, so
# existence there says "this is a Go module", not "this is meant to be depended
# on" — servers and CLIs carry the entry exactly as libraries do. Reduced below
# the threshold so it corroborates (tree.go_importable, a go-library topic, a
# "library" description) but never carries the label alone. This is the same
# correction the MCP signal received in 2.3.1, for the same reason: the token
# records a mechanism, not a decision.
_GO_PROXY_WEIGHT = 3.0


def _collect_distribution(data: RepoData, ledger: _Ledger) -> None:
    for package in data.ecosystem.packages:
        if not package.exists or package.matches_repo is False:
            continue
        # Something installable exists and this repository owns it. That is a
        # claim about installability, not about being importable — the declared
        # tier is what rules `library` back out for a tool or a web app.
        weight = (
            _GO_PROXY_WEIGHT if package.ecosystem == "go" else TIER_WEIGHT["distribution"]
        )
        ledger.add(
            "library",
            "distribution",
            weight,
            f"registry:{package.ecosystem}",
        )
        declared = (package.declared_type or "").strip().lower()
        if declared in REGISTRY_TYPE_RULES:
            ledger.apply(
                REGISTRY_TYPE_RULES[declared], "distribution", f"registry_type:{declared}"
            )
        for category in package.categories:
            key = category.strip().lower()
            if key in REGISTRY_CATEGORY_RULES:
                ledger.apply(
                    REGISTRY_CATEGORY_RULES[key], "distribution", f"registry_category:{key}"
                )


def _collect_structure(data: RepoData, ledger: _Ledger) -> None:
    for token in data.artifacts.structure:
        rules = STRUCTURE_RULES.get(token)
        if rules:
            ledger.apply(rules, "structure", token)
    # Two structural facts the AI-readiness scan already records.
    #
    # The MCP signal needs corroboration and does not carry the label alone. It
    # fires on `.mcp.json` — which is how an editor is *configured to call*
    # MCP servers, not how one is written — and on any dependency whose name
    # ends in "mcp", which a client declares exactly as a server does. Measured
    # on the record at weight 6.0, it alone produced 1,746 of 3,115
    # `mcp-server` labels, freeCodeCamp among them.
    if data.ai_readiness.has_mcp_signal:
        ledger.add("mcp-server", "structure", 3.0, "mcp_signal")
    if data.ai_readiness.api_schema_files:
        ledger.add("network-service", "structure", 3.0, "api_schema")


def _collect_dependencies(data: RepoData, ledger: _Ledger) -> None:
    # A repository that depends on itself says nothing about how it is used.
    # Monorepos do this constantly — Sinatra's own Gemfile requires sinatra,
    # which otherwise reads as "this project runs a web framework".
    own = {package.name.strip().lower() for package in data.ecosystem.packages}
    own |= {
        declaration.name.strip().lower()
        for declaration in data.artifacts.declarations
        if declaration.name
    }
    for dependency in data.dependencies.dependencies:
        name = dependency.name.strip().lower()
        if name in own:
            continue
        label = DEPENDENCY_RULES.get(name)
        if label:
            ledger.add(
                label, "dependencies", TIER_WEIGHT["dependencies"], f"dep:{dependency.name}"
            )


# Tags naming a code-quality tool, mapped to `cli` — but only conditionally.
# A repository tagged `linter` or `formatter` almost always ships a runnable
# checker (eslint, rubocop, go-critic, prettier). The exception is systematic
# rather than random: rule packs and configs for a *host* linter carry the same
# tags — measured on the record, 42 of 256 linter-tagged repositories also
# carry plugin-shaped topics — and there the runnable tool is the host, not
# this repository. So these contribute nothing whenever any independent
# evidence marks the repository as a host extension; see ``classify``.
TOOL_TAGS: frozenset[str] = frozenset({"linter", "lint", "formatter", "code-formatter"})
TOOL_DESCRIPTION_RE = re.compile(r"\b(linter|formatter)\b", re.I)


def _collect_tags(data: RepoData, ledger: _Ledger, guarded: set[str]) -> None:
    tags = list(data.repo.topics)
    for package in data.ecosystem.packages:
        if package.exists and package.matches_repo is not False:
            tags += package.keywords
    for tag in tags:
        key = tag.strip().lower()
        label = TAG_RULES.get(key)
        if label:
            ledger.add(label, "tags", TIER_WEIGHT["tags"], f"tag:{key}")
        elif key in TOOL_TAGS:
            source = f"tag:{key}"
            ledger.add("cli", "tags", TIER_WEIGHT["tags"], source)
            guarded.add(source)


def _collect_description(data: RepoData, ledger: _Ledger, guarded: set[str]) -> None:
    text = data.repo.description or ""
    if not text:
        return
    for pattern, label in DESCRIPTION_RULES:
        if pattern.search(text):
            ledger.add(label, "description", TIER_WEIGHT["description"], f"description:{label}")
    if TOOL_DESCRIPTION_RE.search(text):
        ledger.add("cli", "description", TIER_WEIGHT["description"], "description:tool")
        guarded.add("description:tool")


def _confidence(
    primary: Optional[str], scores: dict[str, float], evidence: list[ClassificationEvidence]
) -> str:
    if primary is None:
        return "none"
    if any(e.label == primary and e.tier == "declared" and e.weight > 0 for e in evidence):
        return "high"
    branch = scores.get(primary, 0.0) + scores.get(SUBTYPE_PARENT.get(primary, ""), 0.0)
    if branch >= 6.0:
        return "medium"
    return "low"


def declared_application(classification: Classification) -> bool:
    """Did a build manifest declare that this repository builds an executable?

    True only on *declared-tier* evidence for the application branch — an npm
    ``bin``, a ``console_scripts`` entry point, an ``<OutputType>`` of Exe, a
    Maven ``war``. This is the standing rule for letting classification touch
    scoring at all: manifest declarations gate scores; labels inferred from
    structure, tags or descriptions never do. The Ledger records what a
    manifest said even when weaker tiers said it louder, so this cannot be
    faked by topics.
    """
    return any(
        evidence.tier == "declared"
        and evidence.weight > 0
        and (
            evidence.label == "application"
            or SUBTYPE_PARENT.get(evidence.label) == "application"
        )
        for evidence in classification.evidence
    )


def classify(data: RepoData) -> Classification:
    """What this repository builds, from stored facts only.

    Pure and total: it reads ``data`` and nothing else, so a rescore reclassifies
    every stored report without touching the network, and a report predating the
    artifact scan still yields whatever its topics, dependencies and registry
    entries support.
    """
    ledger = _Ledger()
    artifacts = _collect_declared(data, ledger)
    _collect_distribution(data, ledger)
    _collect_structure(data, ledger)
    _collect_dependencies(data, ledger)
    guarded: set[str] = set()
    _collect_tags(data, ledger, guarded)
    _collect_description(data, ledger, guarded)

    # The tool-tag guard. Guarded sources only ever argue for `cli`, and
    # host-extension evidence only ever comes from elsewhere, so there is no
    # circularity in letting the latter veto the former.
    host_ext_labels = {"host-extension"} | {
        sub for sub, parent in SUBTYPE_PARENT.items() if parent == "host-extension"
    }
    if guarded and any(
        e.weight > 0 and e.label in host_ext_labels for e in ledger.evidence()
    ):
        ledger.remove(guarded)

    scores = ledger.scores()
    evidence = ledger.evidence()
    labels, top = _resolve(scores)
    primary = labels[0] if labels else None

    return Classification(
        labels=labels,
        top=top,
        primary=primary,
        consumed_by_code="library" in top,
        runs_as_process="application" in top,
        host_extension="host-extension" in top,
        confidence=_confidence(primary, scores, evidence),
        scores={label: round(score, 1) for label, score in sorted(scores.items()) if score > 0},
        evidence=evidence,
        artifacts=artifacts,
    )
