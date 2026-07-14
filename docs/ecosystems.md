# Package-ecosystem integration

The scanner identifies the package(s) a repository publishes and pulls facts
from the package registry. Registry data is richer and more
adoption-relevant than GitHub stars — real download counts, publish cadence,
and deprecation flags — and feeds two metrics (`ecosystem_adoption`,
`package_maintenance`) plus the `data.ecosystem` section of the report.

Implementation: [`src/scanner/ecosystems.py`](../src/scanner/ecosystems.py).

## How a package is identified

1. The scanner reads the repository's own **manifest files** (root or one
   level deep) straight from `raw.githubusercontent.com` — no guessing from
   the repo name.
2. It parses the declared package name out of each manifest.
3. It queries the matching registry for that name.
4. It cross-checks the registry's declared repository URL against the scanned
   repo. A mismatch is recorded (`matches_repo: false`), a warning is emitted,
   and that package is **excluded from scoring** (guards against name-squatting
   and vendored manifests). When the registry declares no repository URL, the
   package is trusted (`matches_repo: null`) since its manifest lives in the repo.

| Ecosystem | Manifest(s) read | Name field |
| --------- | ---------------- | ---------- |
| PyPI | `pyproject.toml`, `setup.cfg` | `[project].name` / `[tool.poetry].name` / `[metadata] name` |
| npm | `package.json` | `.name` (skipped if `"private": true`) |
| Packagist | `composer.json` | `.name` (`vendor/package`) |
| crates.io | `Cargo.toml` | `[package].name` |
| RubyGems | `*.gemspec` | `spec.name = "…"` |
| Hex | `mix.exs` | `app: :…` |

## Registry endpoints

| Ecosystem | Metadata endpoint | Downloads source |
| --------- | ----------------- | ---------------- |
| PyPI | `pypi.org/pypi/{name}/json` | `pypistats.org/api/packages/{name}/recent` (last month) |
| npm | `registry.npmjs.org/{name}` | `api.npmjs.org/downloads/point/last-month/{name}` |
| Packagist | `packagist.org/packages/{name}.json` | in metadata (`downloads.monthly`) |
| crates.io | `crates.io/api/v1/crates/{name}` | in metadata (`recent_downloads`, ÷3 ≈ monthly) |
| RubyGems | `rubygems.org/api/v1/gems/{name}.json` + `/versions/{name}.json` | in metadata (`downloads`, **lifetime total only** — no monthly figure) |
| Hex | `hex.pm/api/packages/{name}` | in metadata (`downloads.recent`, ÷3 ≈ monthly) |

All calls are unauthenticated and best-effort. Any failure degrades to a
warning and the affected fields become `null`; a registry outage never aborts
a scan. crates.io calls send a descriptive `User-Agent` per its crawler policy.

## Feature availability matrix

What each registry reliably provides and the scanner captures. ✓ = captured,
· = not offered by that registry / not captured.

| Field (`EcosystemPackage`)   | PyPI | npm | Packagist | crates.io | RubyGems | Hex |
| ---------------------------- | :--: | :-: | :-------: | :-------: | :------: | :-: |
| `latest_version`             |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |
| `latest_published_at`        |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |
| `days_since_latest_publish`  |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |
| `first_published_at`         |  ✓   |  ✓  |     ·     |     ✓     |    ✓     |  ✓  |
| `versions_count`             |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |
| `monthly_downloads`          | ✓ ¹  |  ✓  |     ✓     |    ✓ ²    |    ·⁶    | ✓ ⁷ |
| `total_downloads`            |  ·   |  ·  |     ✓     |     ✓     |    ✓     |  ✓  |
| `dependents_count`           |  ·   |  ·  |    ✓ ³    |     ·     |    ·     |  ·  |
| `license`                    |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |
| `maintainers_count`          |  ·   |  ✓  |     ·     |     ·     |    ·     |  ·  |
| `is_deprecated`              |  ·   | ✓ ⁴ |    ✓ ⁵    |     ·     |    ·     | ✓ ⁸ |
| `latest_version_yanked`      |  ✓   |  ·  |     ·     |     ✓     |    ·     |  ·  |
| `repository_url` (for match) |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |

1. PyPI downloads come from **pypistats.org**, which rate-limits aggressively
   (HTTP 429). When throttled, `monthly_downloads` is `null` for that scan —
   rerun later. There is no other free monthly-download source for PyPI.
2. crates.io exposes a ~90-day `recent_downloads`; the scanner divides by 3 to
   approximate a month.
3. Packagist reports dependents in its package JSON when available.
4. npm marks deprecation on the latest version (`deprecated` message).
5. Packagist marks abandonment (`abandoned`, optionally naming a replacement).
6. RubyGems exposes only a **lifetime total** download count (no time window).
   The `ecosystem_adoption` metric falls back to `total_downloads` (higher
   saturation) when no `monthly_downloads` is available.
7. Hex exposes a ~90-day `downloads.recent`; the scanner divides by 3 to
   approximate a month.
8. Hex marks retired releases (`retirements`); a retired latest release counts
   as deprecated.

## Metrics fed by ecosystem data

Both are `null` (excluded, weights renormalized) for repos that publish no
package, so non-package repos are never penalized. See
[metrics.md](metrics.md) for full formulas.

- **`ecosystem_adoption`** (Community & Adoption) — monthly downloads
  (log-scaled), falling back to lifetime total downloads for registries that
  report no monthly figure, and — where reported — registry dependents. Real
  installs are a stronger adoption signal than stars.
- **`package_maintenance`** (Sustainability & Governance) — published &
  resolvable, publish recency, version history, and not deprecated/abandoned/
  yanked. Registry upkeep is distinct from GitHub activity.

When a repo publishes several packages (e.g. a monorepo), download counts are
summed and the most recent publish / worst deprecation flag wins.

## Multi-ecosystem repositories

A repository can legitimately belong to several ecosystems at once — a Rust
core with Python bindings and an npm wrapper is one codebase publishing to
crates.io, PyPI and npm. Scoring already handles this (downloads are summed
across all matching packages), but wherever a repository's ecosystems are
*listed* — report chips, catalogue cards, the catalogue's "main" ecosystem —
the order comes from `ranked_ecosystems()` (`ecosystems.py`), by evidence
strength rather than alphabet:

1. **Published ecosystems first**, ordered by combined monthly downloads,
   then total downloads, then name. A package whose registry entry points at
   a *different* repository is excluded here (same rule scoring applies) —
   the manifest names a package this repo does not own.
2. **Manifest-only ecosystems** (a manifest exists but no fetchable published
   package, e.g. Go/Maven/NuGet or a private `package.json`) follow,
   alphabetically.

The first entry is therefore the ecosystem where the repository demonstrably
ships and is most installed, never an alphabetical accident.

## Declared dependencies

Alongside identifying the package a repo *publishes*, the scanner also parses
what it *depends on* — straight out of the same manifest text already fetched
above, with **no additional network calls**. Each entry
(`data.dependencies.dependencies`, model `Dependency`) reports the ecosystem,
package name, the version constraint exactly as declared (e.g. `^3.1.50`,
`>=2.0,<3`), and which manifest it came from.

| Ecosystem | Manifest | Section read |
| --------- | -------- | ------------- |
| PyPI | `pyproject.toml` | `[project].dependencies` (PEP 508) or `[tool.poetry.dependencies]` |
| PyPI | `setup.cfg` | `[options] install_requires` |
| npm | `package.json` | `.dependencies` |
| Packagist | `composer.json` | `.require` |
| crates.io | `Cargo.toml` | `[dependencies]` |
| Go | `go.mod` | `require (…)` blocks and lines (skips `// indirect`) |
| Maven | `pom.xml` | `<dependencies>` (skips `test`/`provided`/`system` scope) |
| RubyGems | `Gemfile` | `gem "…"` lines (skips `:development`/`:test` groups) |
| NuGet | `*.csproj` | `<PackageReference>` (attribute or child `<Version>`) |
| Hex | `mix.exs` | `deps` list `{:name, "…"}` (skips `only: :dev`/`:test`) |

Dev/test dependency groups (`devDependencies`, Poetry's
`[tool.poetry.group.*.dependencies]`, `require-dev`, Gemfile dev/test groups,
Elixir `only: :test`), Go `// indirect` transitive requires, and platform
pseudo-packages (`php`, `ext-*`, `lib-*`) are excluded — they describe the
build/test environment, not what ships.

This is **reported as declared, not resolved**: no registry lookup, no
freshness check against the latest release, no vulnerability scan. That is
tracked separately below.

## Coverage summary

| Ecosystem | Declared dependencies | Published-package registry facts |
| --------- | :-------------------: | :------------------------------: |
| PyPI, npm, Packagist, crates.io | ✓ | ✓ |
| RubyGems | ✓ (`Gemfile`) | ✓ (from `*.gemspec`) |
| Hex | ✓ | ✓ |
| Go, Maven, NuGet | ✓ | · (no registry adapter yet) |

Go, Maven and NuGet contribute a dependency list but no published-package
metrics yet — Go and Maven expose no download counts, and NuGet's published
identifier is rarely declared in the `.csproj`.

## Not yet integrated

- **Registry facts for Go / Maven / NuGet** — a `map_*`/`fetch_*` pair each
  (e.g. the Go module proxy, Maven Central search, the NuGet v3 API). Go and
  Maven lack download stats, so they would feed `package_maintenance`
  (publish recency) but not `ecosystem_adoption`.
- **More manifest types** — `build.gradle*` (Gradle), `Podfile` (CocoaPods),
  `pubspec.yaml` (Dart/Flutter), `*.gemspec` runtime deps.
- **Dependency freshness & known CVEs** — resolving each declared dependency
  against its registry (how far behind the pin is) and cross-referencing
  vulnerability databases. See the roadmap in [metrics.md](metrics.md).
