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

## Registry endpoints

| Ecosystem | Metadata endpoint | Downloads source |
| --------- | ----------------- | ---------------- |
| PyPI | `pypi.org/pypi/{name}/json` | `pypistats.org/api/packages/{name}/recent` (last month) |
| npm | `registry.npmjs.org/{name}` | `api.npmjs.org/downloads/point/last-month/{name}` |
| Packagist | `packagist.org/packages/{name}.json` | in metadata (`downloads.monthly`) |
| crates.io | `crates.io/api/v1/crates/{name}` | in metadata (`recent_downloads`, ÷3 ≈ monthly) |

All calls are unauthenticated and best-effort. Any failure degrades to a
warning and the affected fields become `null`; a registry outage never aborts
a scan. crates.io calls send a descriptive `User-Agent` per its crawler policy.

## Feature availability matrix

What each registry reliably provides and the scanner captures. ✓ = captured,
· = not offered by that registry / not captured.

| Field (`EcosystemPackage`)   | PyPI | npm | Packagist | crates.io |
| ---------------------------- | :--: | :-: | :-------: | :-------: |
| `latest_version`             |  ✓   |  ✓  |     ✓     |     ✓     |
| `latest_published_at`        |  ✓   |  ✓  |     ✓     |     ✓     |
| `days_since_latest_publish`  |  ✓   |  ✓  |     ✓     |     ✓     |
| `first_published_at`         |  ✓   |  ✓  |     ·     |     ✓     |
| `versions_count`             |  ✓   |  ✓  |     ✓     |     ✓     |
| `monthly_downloads`          | ✓ ¹  |  ✓  |     ✓     |    ✓ ²    |
| `total_downloads`            |  ·   |  ·  |     ✓     |     ✓     |
| `dependents_count`           |  ·   |  ·  |    ✓ ³    |     ·     |
| `license`                    |  ✓   |  ✓  |     ✓     |     ✓     |
| `maintainers_count`          |  ·   |  ✓  |     ·     |     ·     |
| `is_deprecated`              |  ·   | ✓ ⁴ |    ✓ ⁵    |     ·     |
| `latest_version_yanked`      |  ✓   |  ·  |     ·     |     ✓     |
| `repository_url` (for match) |  ✓   |  ✓  |     ✓     |     ✓     |

1. PyPI downloads come from **pypistats.org**, which rate-limits aggressively
   (HTTP 429). When throttled, `monthly_downloads` is `null` for that scan —
   rerun later. There is no other free monthly-download source for PyPI.
2. crates.io exposes a ~90-day `recent_downloads`; the scanner divides by 3 to
   approximate a month.
3. Packagist reports dependents in its package JSON when available.
4. npm marks deprecation on the latest version (`deprecated` message).
5. Packagist marks abandonment (`abandoned`, optionally naming a replacement).

## Metrics fed by ecosystem data

Both are `null` (excluded, weights renormalized) for repos that publish no
package, so non-package repos are never penalized. See
[metrics.md](metrics.md) for full formulas.

- **`ecosystem_adoption`** (Community & Adoption) — monthly downloads
  (log-scaled) and, where reported, registry dependents. Real installs are a
  stronger adoption signal than stars.
- **`package_maintenance`** (Sustainability & Governance) — published &
  resolvable, publish recency, version history, and not deprecated/abandoned/
  yanked. Registry upkeep is distinct from GitHub activity.

When a repo publishes several packages (e.g. a monorepo), download counts are
summed and the most recent publish / worst deprecation flag wins.

## Not yet integrated

Detected as manifests (see `DependencySignals.ecosystems`) but without a
registry adapter yet: **Go modules**, **Maven**, **RubyGems**, **NuGet**,
**Hex**. Adding one means implementing a parser + a `map_*`/`fetch_*` pair and
extending the availability matrix above.
