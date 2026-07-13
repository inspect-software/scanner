# Report schema

**Schema version: 0.9.0** (`schema_version` field in every report).
The schema is defined as Pydantic models in
[`src/scanner/models.py`](../src/scanner/models.py); this document describes
it for consumers. Any breaking structural change bumps `schema_version`.

## Data vs. metrics

A report has two strictly separated layers:

| Layer     | Field      | What it is | Versioned by |
| --------- | ---------- | ---------- | ------------ |
| **Data**    | `data`    | Raw facts observed from public sources (GitHub API). No judgement, no scoring — reported as-is. | `schema_version` |
| **Metrics** | `metrics` | Standardized scores (integers **1..100**) computed from `data` by deterministic, documented formulas. | `metrics.metrics_version` |

The separation is deliberate: data is reproducible ground truth; metrics are
an *interpretation* of it. Consumers who disagree with our methodology can
recompute their own scores from `data` alone. Scoring formulas are documented
in [metrics.md](metrics.md).

## Report types

The scanner produces two report types, discriminated by the `report_type`
field: `"repository"` (default) and `"organization"`. Both share the same
data/metrics layering, the `Metric` object shape, and the band scale.

## Top level (repository report)

```jsonc
{
  "report_type": "repository",
  "schema_version": "0.9.0",
  "generated_at": "2026-07-06T12:00:00Z",   // UTC timestamp of the scan
  "source": { ... },                          // what was scanned
  "config": { ... },                          // scan configuration (see below)
  "data": { ... },                            // raw facts (data layer)
  "metrics": { ... },                         // 1..100 scores (metrics layer)
  "warnings": ["..."]                         // non-fatal collection problems
}
```

`warnings` lists data that could not be collected (rate limits, GitHub still
computing statistics, truncated file trees). A warning means the related
fields are `null`/incomplete — affected metrics exclude those inputs rather
than scoring them as zero.

## `config` — scan configuration

Records which parts of the methodology were active for this scan, so the score
is reproducible. Empty collections mean the full methodology (everything
enabled). Disabled items are removed from scoring with the remaining weights
renormalized (see [metrics.md](metrics.md#configuration-enabling--disabling-metrics)).

```jsonc
"config": {
  "disabled_categories": ["security"],           // category keys
  "disabled_metrics": ["popularity"],            // metric keys
  "disabled_components": {                         // metric key -> component names
    "documentation": ["Wiki"]
  }
}
```

## `source`

| Field   | Type   | Description |
| ------- | ------ | ----------- |
| `url`   | string | Input as given by the user |
| `host`  | string | Always `github.com` for now |
| `owner` | string | Repository owner/organization |
| `name`  | string | Repository name |

## `data`

### `data.owner` — owning account profile (repository reports)

Public profile of the account that owns the repository — **organization or
user**. `null` only when the profile could not be fetched.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `login`, `type` | string | Account login and `"User"` / `"Organization"` |
| `name`, `company`, `blog` | string? | Profile fields |
| `followers`, `public_repos` | int | Reach and portfolio size |
| `created_at`, `account_age_days` | | Account track record |
| `is_verified` | bool? | Verified-domain badge (organizations only; `null` for users) |
| `avatar_url` | string? | |

Unlike other `data`, this one **does** feed a score: the `stewardship` metric
(see metrics.md) reads it to reward organization backing. The facts themselves
are still just observations.

### `data.repo` — repository metadata

| Field | Type | Description |
| ----- | ---- | ----------- |
| `owner_type` | string? | `"User"` or `"Organization"` |
| `description`, `homepage` | string? | From the repo profile |
| `has_wiki` | bool? | Wiki enabled (feeds the documentation metric) |
| `created_at`, `updated_at`, `pushed_at` | datetime? | Repo lifecycle timestamps |
| `default_branch` | string? | Branch the file-tree signals were read from |
| `is_fork`, `is_archived`, `is_disabled` | bool | Status flags |
| `size_kb` | int? | Repo size reported by GitHub |
| `primary_language` | string? | GitHub's primary language |
| `languages` | object | Language → bytes of code |
| `topics` | string[] | Repository topics |
| `license_spdx` | string? | SPDX id of the detected license (`null` if none/unrecognized) |

### `data.popularity`

| Field | Description |
| ----- | ----------- |
| `stars`, `forks` | Standard GitHub counters |
| `watchers` | Subscribers (notification watchers, not the legacy stars alias) |
| `open_issues_and_prs` | GitHub's combined open issues + open PRs counter |

### `data.activity`

| Field | Description |
| ----- | ----------- |
| `commits_last_year` | Total commits in the last 52 weeks (all contributors) |
| `active_weeks_last_year` | Weeks with ≥1 commit in the last 52 |
| `days_since_last_push` | Days since the last push to any branch |
| `releases_count` | Releases fetched (capped at 100) |
| `latest_release_tag`, `latest_release_at` | Most recent release |
| `mean_days_between_releases` | Mean gap between the most recent releases (up to 10) |

### `data.maintainership`

| Field | Description |
| ----- | ----------- |
| `contributors_sampled` | Contributors counted (top 100 by commits; anonymous excluded) |
| `top_contributors` | Up to 10 `{login, commits}` entries |
| `bus_factor` | Smallest number of contributors covering ≥50% of sampled commits |
| `top_contributor_share` | Share of sampled commits by the top contributor (0..1) |
| `issues.open_issues`, `issues.closed_issues` | Issue counts (search API) |
| `issues.closed_ratio` | closed / (open + closed); `null` if the repo has no issues |
| `issues.open_prs`, `issues.merged_prs`, `issues.closed_unmerged_prs` | PR counts |

### `data.community` — GitHub community profile

`health_percentage` plus boolean presence flags: `has_readme`, `has_license`,
`has_contributing`, `has_code_of_conduct`, `has_issue_template`,
`has_pull_request_template`, `has_description`.

### `data.quality_signals` — file-tree heuristics

| Field | Detection |
| ----- | --------- |
| `has_ci`, `ci_workflows` | `.github/workflows/*.yml\|yaml` |
| `has_tests` | `test(s)`/`spec(s)`/`__tests__` directories or test-file naming patterns |
| `has_docs_dir` | Non-empty `docs/` or `doc/` directory |
| `has_linter_config`, `linter_configs` | Known linter config files (ruff, flake8, eslint, golangci, …) |
| `has_editorconfig`, `has_precommit_config` | `.editorconfig`, `.pre-commit-config.yaml` |

File-tree signals indicate **presence, not quality** — they come from the git
tree of the default branch, without cloning or executing anything.

### `data.security_signals`

| Field | Detection |
| ----- | --------- |
| `has_security_policy` | `SECURITY.md` (root, `.github/`, or `docs/`) |
| `has_dependabot_config` | `.github/dependabot.yml\|yaml` |
| `has_codeql_workflow` | Workflow filename containing `codeql` |
| `lockfiles` | Known lockfiles (`uv.lock`, `package-lock.json`, `Cargo.lock`, …) |
| `scorecard` | OpenSSF Scorecard result (below); `null` when the `scorecard` CLI didn't run |

The file-based fields above are the coarse **fallback** signal. The primary
security signal is `scorecard`, produced by the open-source
[OpenSSF Scorecard](https://github.com/ossf/scorecard) CLI:

```jsonc
"scorecard": {
  "aggregate_score": 6.3,          // Scorecard's headline 0..10 (null if it couldn't compute)
  "scorecard_version": "v5.0.0",
  "ran_at": "2026-07-06T00:00:00Z",
  "commit": "…",                    // repo commit Scorecard evaluated
  "checks": [
    { "name": "Token-Permissions", "score": 0,    // 0..10, or null when inconclusive (Scorecard -1)
      "reason": "…", "documentation_url": "https://…" }
  ]
}
```

A check `score` of `null` means Scorecard could not determine it (its `-1`);
the `security_posture` metric **excludes** such checks and renormalizes rather
than scoring them zero. See
[metrics.md](metrics.md#security) and [ecosystems.md](ecosystems.md) — Scorecard
setup is documented in the README.

### `data.dependencies`

| Field | Description |
| ----- | ----------- |
| `manifests` | Dependency manifests found at root or one level deep |
| `ecosystems` | Ecosystems inferred from manifests (`pypi`, `npm`, `packagist`, `crates`, `go`, `maven`, `rubygems`, `nuget`, `hex`) |
| `dependencies` | Declared dependencies, parsed straight from manifest text (see below) |

`dependencies` is a list of `Dependency` objects — one per declared runtime
dependency, for the five manifest types the scanner already reads
(`pyproject.toml`, `setup.cfg`, `package.json`, `composer.json`,
`Cargo.toml`). Dev/test groups and platform pseudo-packages are excluded. See
[ecosystems.md](ecosystems.md#declared-dependencies).

| Field | Type | Description |
| ----- | ---- | ----------- |
| `ecosystem`, `name` | string | Ecosystem and package identifier |
| `version_constraint` | string? | As declared in the manifest, verbatim (e.g. `"^3.1.50"`); `null` if unpinned |
| `manifest` | string | Which manifest file declared it |

This is reported **as declared, not resolved** — no registry lookup for the
dependency itself, no freshness check, no vulnerability scan (roadmap item in
[metrics.md](metrics.md#roadmap-not-yet-scored)).

### `data.ecosystem` — published package facts (from registries)

`ecosystem.packages` is a list of the packages this repo publishes, with facts
pulled from the package registry (PyPI, npm, Packagist, crates.io). See
[ecosystems.md](ecosystems.md) for identification, endpoints, and the
per-ecosystem availability matrix.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `ecosystem`, `name` | string | Registry and package id |
| `registry_url` | string | Human-facing registry page |
| `matches_repo` | bool? | Registry's repo URL points back to this repo (`null` if none declared); `false` → excluded from scoring |
| `latest_version`, `latest_published_at`, `days_since_latest_publish` | | Latest release on the registry |
| `first_published_at`, `versions_count` | | Publish history |
| `monthly_downloads`, `total_downloads`, `dependents_count` | int? | Adoption (availability varies by ecosystem) |
| `license`, `maintainers_count` | | |
| `is_deprecated`, `deprecation_note`, `latest_version_yanked` | | Deprecation / abandonment / yank flags |
| `repository_url` | string? | Repo URL the registry declares |

This section **feeds** the `ecosystem_adoption` and `package_maintenance`
metrics; the facts themselves remain plain observations.

### `data.ai_readiness` — AI-agent readiness signals

Presence- and size-based heuristics from the file tree (paths + blob sizes, no
file contents) describing how well the repo is set up for AI coding agents.
These feed the weight-0 **AI Readiness** badge (see
[metrics.md](metrics.md#ai-readiness)); they never affect the overall score.

| Field | Type | Detection |
| ----- | ---- | --------- |
| `agent_instruction_files` | string[] | `CLAUDE.md`, `AGENTS.md`, `.cursor/rules`, `.github/copilot-instructions.md`, `GEMINI.md`, `.windsurfrules`, … |
| `agent_instruction_max_bytes` | int? | Size of the largest such file (stub detection) |
| `has_llms_txt` | bool | `llms.txt` / `llms-full.txt` present |
| `bootstrap_files` | string[] | One-command bootstrap / task runners (Makefile, Taskfile, justfile, mise, noxfile) |
| `typecheck_configs` | string[] | `mypy.ini`, `pyrightconfig.json`, `tsconfig.json`, `py.typed`, … |
| `has_devcontainer`, `has_dockerfile`, `has_nix` | bool | Reproducible-environment signals |
| `api_schema_files` | string[] | OpenAPI/Swagger, GraphQL SDL, protobuf, AsyncAPI |
| `has_mcp_signal` | bool | Model Context Protocol server dependency or `mcp.json` |
| `example_dirs` | string[] | `examples/` · `recipes/` · `samples/` directories, notebooks |
| `source_files_sampled` | int | Non-vendored source files considered for the size signal |
| `oversized_source_files` | int | Source files above the agent-legibility size threshold (~60 KB) |
| `largest_source_bytes` | int? | Size of the largest sampled source file |

## `metrics`

See [metrics.md](metrics.md) for the full methodology. Metrics are grouped
into weighted **categories**, each with its own rolled-up score:

```jsonc
{
  "metrics_version": "1.0.0",
  "overall": { /* Metric — weighted mean of the categories */ },
  "categories": [
    {
      "key": "vitality",
      "name": "Vitality",
      "description": "...",
      "weight": 0.22,            // weight in the overall score
      "value": 75,               // category rollup, 1..100 (null if unscored)
      "band": "good",
      "metrics": [ /* Metric objects in this category */ ]
    }
    // community, governance, engineering, security ...
  ]
}
```

Categories present in a repository report: `vitality`, `community`,
`governance`, `engineering`, `security` (a category with no scorable metric is
omitted). `overall.inputs` holds the per-category values that fed the mean.

To find a single metric, scan `categories[].metrics[]` for a matching `key`
(the `Metrics.by_key()` / `Metrics.category()` helpers do this in code).

Each metric object:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `key` | string | Stable machine identifier |
| `name` | string | Human-readable name |
| `value` | int | Score, always **1..100**, higher is better |
| `band` | string | Standardized interval: `critical` / `at_risk` / `moderate` / `good` / `excellent` |
| `components` | array | Per-criterion breakdown of the score (see below) |
| `inputs` | object | The raw data values the score was computed from (transparency/audit trail) |
| `note` | string? | Caveats, e.g. components excluded due to missing data |

Each entry in `components`:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `name` | string | Criterion name (matches docs/metrics.md) |
| `points` | float | Points earned (0 when excluded) |
| `max_points` | float | Weight of the criterion within the metric |
| `status` | string | `met` (full points) / `partial` (some) / `missed` (zero) / `excluded` (no data or not applicable; removed from scoring, weights renormalized) |
| `detail` | string? | The observed value behind the outcome, e.g. `"last push 2 days ago"` |

The metric's `value` is exactly `round(100 × Σpoints / Σmax_points)` over the
non-excluded components (clamped to 1..100) — the breakdown *is* the score.
Category and `overall` rollups have no components; their `inputs` carry the
child values (metric values for a category, category values for overall).

A metric is `null` when none of its inputs could be collected — **missing
data is never silently scored**.

## Organization report

Produced when the scan target is an organization (`inspect-scan orgname`).

```jsonc
{
  "report_type": "organization",
  "schema_version": "0.9.0",
  "generated_at": "...",
  "source": { "url": "...", "host": "github.com", "login": "psf" },
  "config": { /* same ScanConfig shape as repository reports */ },
  "data": {
    "info": { /* OrgInfo: login, name, description, blog, location, email,
                 twitter_username, is_verified, public_repos, followers,
                 created_at, avatar_url */ },
    "portfolio": {
      "repos_sampled": 42,            // sample: up to 100 public repos, most recently pushed
      "total_stars_sampled": 111970,
      "repos_pushed_90d": 14,
      "repos_pushed_365d": 21,
      "original_repos_sampled": 38,   // non-forks in the sample
      "forks_sampled": 4,
      "top_repos": [ { "name": "...", "stars": 0, "pushed_at": "...", "description": "..." } ],
      "public_members": 28            // capped at 100
    }
  },
  "metrics": {
    "metrics_version": "1.0.0",
    "overall": { /* Metric */ },
    "categories": [
      { "key": "activity_reach", "name": "Activity & Reach", "weight": 0.75, "value": 72,
        "metrics": [ /* portfolio_activity, community_reach */ ] },
      { "key": "governance", "name": "Governance & Profile", "weight": 0.25, "value": 80,
        "metrics": [ /* profile_completeness */ ] }
    ]
  },
  "warnings": []
}
```

## Storage layout

With storage enabled (`--storage [DIR]` or the `SCANNER_STORAGE` variable),
reports are written under the storage root with standardized names derived
from GitHub identifiers (sanitized to `[a-z0-9._-]`, lowercased):

```
<storage>/
  repos/<owner>__<repo>.json      e.g. repos/nayjest__ai-microcore.json
  repos/<owner>__<repo>.html
  orgs/<login>.json               e.g. orgs/psf.json
  orgs/<login>.html
```

One file pair per target; a rescan overwrites the previous report.
