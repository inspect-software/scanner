# inspect-scanner

CLI scanner that audits a **public GitHub repository or organization** and
produces structured **JSON and HTML reports** with health, maintainability,
quality, security, and dependency signals. Part of the inspect-software
auditing/certification platform.

At this stage the scanner uses only **publicly available GitHub API data** —
no cloning, no code execution.

## Install

```bash
uv sync          # or: pip install -e .
```

## Usage

```bash
inspect-scan https://github.com/pallets/flask
inspect-scan pallets/flask -o report.json
inspect-scan git@github.com:pallets/flask.git --compact

# Single-file HTML report (score-focused, human-readable)
inspect-scan pallets/flask -o report.json --html report.html

# Re-render HTML from a previously saved JSON report — no network needed
inspect-scan report.json --html report.html

# Scan an organization (profile + repository portfolio)
inspect-scan psf
inspect-scan https://github.com/python

# Store JSON + HTML under ./storage with standardized names
inspect-scan pallets/flask --storage
inspect-scan psf --storage D:/audit-reports

# Enable/disable parts of the scoring methodology (see below)
inspect-scan pallets/flask --disable-category security --disable-metric popularity
inspect-scan pallets/flask --config scan-config.json --html report.html
```

### Scan configuration (enabling / disabling metrics)

Any part of the methodology — a component, a metric, or a whole category — can
be switched off for a scan. Disabled items are removed from scoring and the
remaining weights **renormalized** (never counted as zero), so scores stay on
the 1–100 scale. The configuration is embedded in the report (`config`) and
summarized in a **Scan configuration** section of the HTML.

```bash
inspect-scan pallets/flask --disable-category security          # drop a category
inspect-scan pallets/flask --disable-metric popularity          # drop a metric
inspect-scan pallets/flask --disable-component documentation:Wiki  # drop a component
inspect-scan pallets/flask --config scan-config.json            # from a file (+ flags merge on top)
```

```jsonc
// scan-config.json
{
  "disabled_categories": ["security"],
  "disabled_metrics": ["popularity"],
  "disabled_components": { "documentation": ["Wiki"] }
}
```

Category/metric keys and component names are listed in
[docs/metrics.md](docs/metrics.md#configuration-enabling--disabling-metrics).

### OpenSSF Scorecard (security metric)

The **Security** metric is backed by [OpenSSF Scorecard](https://github.com/ossf/scorecard),
a neutral, tool-agnostic security standard — so a project earns credit for the
*practice* (any dependency-update tool, any SAST, signed releases, least-
privilege workflow tokens, no known-vulnerable deps…), not for a specific
vendor's config file. Checks Scorecard can't determine are excluded from the
score, never counted as zero.

This needs the `scorecard` CLI on your `PATH`:

```bash
# e.g. via Go, Homebrew, or a release binary — see the Scorecard README
go install github.com/ossf/scorecard/v5@latest
```

The scan reuses the same GitHub token it already resolves. Scorecard is
best-effort: if the binary is missing, times out, or fails, the security metric
falls back to coarse file checks and a warning is emitted. Skip it with
`--no-scorecard` (faster), or disable the whole metric with
`--disable-metric security_posture`.

### Report storage

`--storage [DIR]` writes both report formats into a standardized layout:

```
storage/
  repos/<owner>__<repo>.json|.html    e.g. repos/pallets__flask.html
  orgs/<login>.json|.html             e.g. orgs/psf.html
```

Names are sanitized to filesystem-safe characters and lowercased. The storage
root comes from (highest precedence first): the `--storage DIR` argument, the
`SCANNER_STORAGE` variable (environment or `.env`), then `./storage`. Setting
`SCANNER_STORAGE` enables storage even without the flag. One file pair per
target — a rescan overwrites.

### GitHub token

Unauthenticated GitHub API access is limited to 60 requests/hour (a scan uses
~12). Provide a token to raise the limit to 5000/hour — a fine-grained token
with public-repo read access is enough. The scanner resolves it from, in
order of precedence:

1. `--token` CLI argument
2. `GITHUB_TOKEN` or `GH_TOKEN` environment variable
3. `GITHUB_TOKEN` or `GH_TOKEN` in a `.env` file in the working directory
   (see [.env.example](.env.example); `.env` is gitignored)

```bash
inspect-scan pallets/flask --token ghp_...   # explicit
export GITHUB_TOKEN=ghp_...                  # environment
cp .env.example .env                         # or a local .env file
```

## What's in the report

The report has two strictly separated layers (schema defined as Pydantic
models in [`src/scanner/models.py`](src/scanner/models.py)):

- **`data`** — raw facts observed from the GitHub API: repo metadata, the
  owning account's profile (organization or user), popularity, commit/release
  activity, contributors and bus factor, issue/PR counts, community profile,
  file-tree signals (CI, tests, linting, security policy, lockfiles, dependency
  manifests, and the **declared dependency list** parsed straight from those
  manifests — name + version constraint, no registry lookup yet), and — for
  repos that publish a package — **registry facts** from PyPI / npm /
  Packagist / crates.io (downloads, versions, deprecation). No judgement, no
  scoring.
- **`metrics`** — standardized scores, each an integer **1..100** mapped to a
  band (`critical` / `at_risk` / `moderate` / `good` / `excellent`). Ten
  metrics grouped into five weighted **categories**, each with its own
  rolled-up score, plus a weighted `overall`:

  | Category | Metrics |
  | -------- | ------- |
  | **Vitality** | development_activity, release_discipline |
  | **Community & Adoption** | popularity, community_health, ecosystem_adoption |
  | **Sustainability & Governance** | maintainer_resilience, responsiveness, **stewardship**, package_maintenance |
  | **Engineering Quality** | engineering_practices, documentation |
  | **Security** | security_posture |

  **stewardship** scores who backs the repo: organization-owned projects
  (especially with a GitHub-verified domain and reach) score higher than
  single-personal-account projects. **ecosystem_adoption** and
  **package_maintenance** read real package-registry data (downloads, publish
  recency, deprecation) for repos that publish to PyPI / npm / Packagist /
  crates.io — see [docs/ecosystems.md](docs/ecosystems.md). **security_posture**
  is backed by OpenSSF Scorecard (tool-agnostic; see below). Every metric echoes
  the raw `inputs` it was computed from.

The `--html` flag renders the report into a single-file HTML page focused on
the score: an overall gauge with the standardized band scale, a category radar
chart, a dedicated **Ownership** section, and metric cards grouped by category
with plain-language explanations, per-criterion pass/fail breakdowns, and the
exact inputs used. Styling/charts/icons load from CDNs (Chart.js, Lucide,
Google Fonts); the page degrades gracefully offline.

Both layers are independently versioned (`schema_version` for the structure,
`metrics_version` for the scoring methodology). Full documentation:

- [docs/report-schema.md](docs/report-schema.md) — field-by-field schema
- [docs/metrics.md](docs/metrics.md) — bands, formulas, weights, worked example
- [docs/ecosystems.md](docs/ecosystems.md) — package-registry integration & feature matrix

Notes on semantics:

- File-based signals are heuristics from the git tree of the default branch;
  they indicate *presence*, not quality.
- Metrics are **signals, not warranties** — publicly visible practices, not a
  code audit. Missing data is excluded and renormalized, never scored as zero.

## Development

```bash
uv sync --extra dev
uv run pytest
```

## Exit codes

- `0` — report produced (warnings, if any, go to stderr)
- `1` — GitHub API failure (rate limit, network, repo not found)
- `2` — invalid input (unparseable repo URL)
