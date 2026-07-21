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
| PyPI | `pyproject.toml`, `setup.cfg`, `setup.py` | `[project].name` / `[tool.poetry].name` / `[metadata] name` / literal `name='…'` |
| npm | `package.json` | `.name` (skipped if `"private": true`) |
| Packagist | `composer.json` | `.name` (`vendor/package`) |
| crates.io | `Cargo.toml` | `[package].name` |
| RubyGems | `*.gemspec` | `spec.name = "…"` |
| Hex | `mix.exs`, `gleam.toml`, `*.app.src`, `rebar.config` | `app: :…` / `name = "…"` / `{application, Name, …}` / — (see below) |
| Go | `go.mod` | `module …` directive (must name a real hosting path) |
| Maven | `pom.xml` | `<groupId>:<artifactId>` (groupId may come from `<parent>`; `<packaging>pom</packaging>` skipped) |
| NuGet | `*.csproj` | explicit `<PackageId>`, else the project filename (guessed — see below) |

Hex covers three languages. Elixir declares the app in `mix.exs` and Gleam in
`gleam.toml`; an Erlang project publishes under the name in
`src/<app>.app.src`, never in `rebar.config`, which carries no name at all.

### What is not a published package

At most `MAX_PACKAGES` (8) packages are resolved per scan, and multi-module
repositories blow through that — Presto carries 123 surface manifests. Three
rules decide which eight, and they are all structural: **no rule ever matches
on the package name.** `pytest-asyncio` (234M downloads/month), `pytest-cov`
and `tokio-test` are their repositories' flagship packages, so excluding
"test"-ish names would delete real products.

1. **Build aggregators** — a pom with `<packaging>pom</packaging>` ships no
   code. Every package Keycloak reported was one of these (`keycloak-bom-parent`,
   `keycloak-adapters-pom`, …); Zipkin reported `zipkin-parent` but not
   `zipkin`.
2. **Sample and benchmark directories** — `benchmarks/pom.xml`, `docs/…`, and
   the rest of the noise segments, at every depth.
3. **Ordering, so the budget buys the right eight** — manifests are ranked by
   depth, then path length, before the cap applies. Tree order is alphabetical
   order, which is uncorrelated with importance: Rails reported `actioncable`
   but not `rails`, arrow-rs reported `arrow-arith` but neither `arrow` nor
   `parquet`, and Arthas spent three of its eight slots on demo and
   integration-test modules. Path length stands in for centrality — core
   modules get short directory names (`core/`, `boot/`) and peripheral ones get
   long descriptive ones (`arthas-demo-external-command/`). A heuristic, but a
   far better one than the alphabet.

### Nested manifests

When nothing on the surface resolves, the scanner makes a second pass over
manifests **exactly two levels deep** — the conventional monorepo layouts
(`src/Foo/Foo.csproj`, `libs/x/pyproject.toml`, `packages/x/package.json`).
Directories holding samples, tests, benchmarks, docs or vendored code are
excluded, and the pass is capped at 20 files.

The pass is deliberately a *fallback*, gated on the surface pass having
produced no package that counts (one naming a different repository does not
count). Descending unconditionally was measured to add an ecosystem to ~1% of
repositories that already had one, while risking a reordering of their ranked
ecosystems and multiplying fetches across the whole catalogue.

### Guessed names

Two conventions put the published name outside the manifest body, so the
scanner guesses it and then demands proof:

- `*.csproj` without a `<PackageId>` — NuGet defaults the id to the project
  filename.
- `rebar.config` with no `src/*.app.src` — the app conventionally takes the
  repository name.

A guessed name is accepted **only** when the registry entry points back at this
repository (`matches_repo: true`). Unlike a declared name, one that merely
names no repository is not enough: without that rule a project directory named
after a popular package would silently adopt a stranger's download counts.

Go special cases: a module is "published" once the path is **tagged** — Go has
no central publish step, the proxy *is* the registry. An untagged `go.mod`
that is a submodule of the scanned repo (e.g. `samples/go.mod`) falls back to
the repo-root module path, whose tags live at the repo root; several manifests
converging on one module are reported once.

## Registry endpoints

| Ecosystem | Metadata endpoint | Downloads source |
| --------- | ----------------- | ---------------- |
| PyPI | `pypi.org/pypi/{name}/json` | `pypistats.org/api/packages/{name}/recent` (last month) |
| npm | `registry.npmjs.org/{name}` | `api.npmjs.org/downloads/point/last-month/{name}` |
| Packagist | `packagist.org/packages/{name}.json` | in metadata (`downloads.monthly`) |
| crates.io | `crates.io/api/v1/crates/{name}` | in metadata (`recent_downloads`, ÷3 ≈ monthly) |
| RubyGems | `rubygems.org/api/v1/gems/{name}.json` + `/versions/{name}.json` | in metadata (`downloads`, **lifetime total only** — no monthly figure) |
| Hex | `hex.pm/api/packages/{name}` | in metadata (`downloads.recent`, ÷3 ≈ monthly) |
| Go | `proxy.golang.org/{module}/@latest` + `/@v/list` + latest `.mod` | none — the Go proxy publishes no download statistics |
| Maven | `repo1.maven.org/maven2/{g}/{a}/maven-metadata.xml` + latest `.pom` | none — Maven Central publishes no download counter |
| NuGet | `azuresearch-usnc.nuget.org/query` + registration `catalogEntry` | in search metadata (`totalDownloads`, **lifetime total only**) |

All calls are unauthenticated and best-effort. Any failure degrades to a
warning and the affected fields become `null`; a registry outage never aborts
a scan. crates.io calls send a descriptive `User-Agent` per its crawler policy.

## Feature availability matrix

What each registry reliably provides and the scanner captures. ✓ = captured,
· = not offered by that registry / not captured.

| Field (`EcosystemPackage`)   | PyPI | npm | Packagist | crates.io | RubyGems | Hex | Go | Maven | NuGet |
| ---------------------------- | :--: | :-: | :-------: | :-------: | :------: | :-: | :-: | :---: | :---: |
| `latest_version`             |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |  ✓  |   ✓   |   ✓   |
| `latest_published_at`        |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |  ✓  |  ✓ ⁹  |  ✓ ¹⁰ |
| `days_since_latest_publish`  |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |  ✓  |  ✓ ⁹  |  ✓ ¹⁰ |
| `first_published_at`         |  ✓   |  ✓  |     ·     |     ✓     |    ✓     |  ✓  |  ·  |   ·   |   ·   |
| `versions_count`             |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |  ✓  |   ✓   |   ✓   |
| `monthly_downloads`          | ✓ ¹  |  ✓  |     ✓     |    ✓ ²    |    ·⁶    | ✓ ⁷ |  ·  |   ·   |   ·   |
| `total_downloads`            |  ·   |  ·  |     ✓     |     ✓     |    ✓     |  ✓  |  ·  |   ·   |   ✓   |
| `dependents_count`           |  ·   |  ·  |    ✓ ³    |     ·     |    ·     |  ·  |  ·  |   ·   |   ·   |
| `license`                    |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |  ·  |   ✓   |   ✓   |
| `maintainers_count`          |  ·   |  ✓  |     ·     |     ·     |    ·     |  ·  |  ·  |   ·   |   ·   |
| `is_deprecated`              |  ·   | ✓ ⁴ |    ✓ ⁵    |     ·     |    ·     | ✓ ⁸ | ✓ ¹¹|   ·   |  ✓ ¹² |
| `latest_version_yanked`      |  ✓   |  ·  |     ·     |     ✓     |    ·     |  ·  |  ·  |   ·   |   ·   |
| `repository_url` (for match) |  ✓   |  ✓  |     ✓     |     ✓     |    ✓     |  ✓  |  ✓  |   ✓   |   ✓   |

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
9. Maven's `maven-metadata.xml` carries one `lastUpdated` stamp that moves on
   each publish — it stands in for the latest version's publish time.
10. NuGet stamps unlisted versions with year 1900; those dates are discarded.
11. Go modules declare deprecation with a `// Deprecated:` comment in the
    latest version's `go.mod`.
12. NuGet deprecation comes from the registration `catalogEntry.deprecation`
    (message and reasons).

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
   package, e.g. a private `package.json` or a `.csproj` with no
   `<PackageId>`) follow, alphabetically.

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

## All dependencies (resolved graph)

Separately from the declared **direct** list, the scanner collects the
repository's **full resolved dependency set** — direct plus
indirect/transitive — from GitHub's precomputed dependency-graph SBOM export
(`GET /repos/{owner}/{repo}/dependency-graph/sbom`, an SPDX document; one API
call per scan). GitHub builds that graph from the repo's manifests *and*
lockfiles, so it includes the transitive closure whenever a lockfile is
committed. Implementation: [`src/scanner/sbom.py`](../src/scanner/sbom.py);
reported in `data.dependencies.all_dependencies`.

- **Direct vs. indirect** is derived by matching each resolved package
  against the declared direct set (names normalized; PEP 503 for PyPI).
  A resolved package matching no declared runtime dependency is reported as
  indirect — this also covers direct *dev/test* dependencies, which the
  declared list intentionally excludes.
- **Excluded**: the repository's own root package and GitHub Actions entries
  (CI workflow dependencies, not part of the shipped software).
- **Reliability**: collection is strictly best-effort. Any failure — the
  dependency graph disabled, a rate limit, a network error, a malformed
  payload — sets `all_dependencies.error` (mirrored in the report `warnings`)
  and the scan continues; the declared direct list is collected independently
  and is never affected. The whole step runs under a hard **5-minute time
  budget** (`TIME_BUDGET_SECONDS`).
- **Bounded reports**: `total_count` / `direct_count` / `indirect_count` are
  always complete, but at most 2,000 packages are embedded in the report
  (direct entries first, `truncated: true` when capped).

Ecosystem differences: GitHub's graph covers all manifests listed above.
For libraries that commit no lockfile (common on npm and PyPI) the graph
contains only what the manifests declare, so the indirect set may be empty or
partial; Go is complete regardless because `go.mod` itself carries the pruned
transitive module set. Maven and NuGet repos rarely commit lockfiles, so their
resolved sets are usually manifest-only.

## Coverage summary

| Ecosystem | Declared dependencies | Published-package registry facts |
| --------- | :-------------------: | :------------------------------: |
| PyPI, npm, Packagist, crates.io | ✓ | ✓ |
| RubyGems | ✓ (`Gemfile`) | ✓ (from `*.gemspec`) |
| Hex | ✓ | ✓ |
| Go, Maven | ✓ | ✓ (no download stats — those registries publish none, so they feed `package_maintenance` but not `ecosystem_adoption`) |
| NuGet | ✓ | ✓ (lifetime downloads; identified from `<PackageId>`, or from the project filename when the registry confirms the repository) |

## Not yet integrated

- **More manifest types** — `build.gradle*` (Gradle), `Podfile` (CocoaPods),
  `pubspec.yaml` (Dart/Flutter), `*.gemspec` runtime deps.
- **Dependency freshness & known CVEs** — resolving each declared dependency
  against its registry (how far behind the pin is) and cross-referencing
  vulnerability databases. See the roadmap in [metrics.md](metrics.md).
