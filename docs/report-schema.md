# Report schema

**Schema version: 0.2.0** (`schema_version` field in every report).
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

## Top level

```jsonc
{
  "schema_version": "0.2.0",
  "generated_at": "2026-07-06T12:00:00Z",   // UTC timestamp of the scan
  "source": { ... },                          // what was scanned
  "data": { ... },                            // raw facts (data layer)
  "metrics": { ... },                         // 1..100 scores (metrics layer)
  "warnings": ["..."]                         // non-fatal collection problems
}
```

`warnings` lists data that could not be collected (rate limits, GitHub still
computing statistics, truncated file trees). A warning means the related
fields are `null`/incomplete — affected metrics exclude those inputs rather
than scoring them as zero.

## `source`

| Field   | Type   | Description |
| ------- | ------ | ----------- |
| `url`   | string | Input as given by the user |
| `host`  | string | Always `github.com` for now |
| `owner` | string | Repository owner/organization |
| `name`  | string | Repository name |

## `data`

### `data.repo` — repository metadata

| Field | Type | Description |
| ----- | ---- | ----------- |
| `description`, `homepage` | string? | From the repo profile |
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

### `data.dependencies`

| Field | Description |
| ----- | ----------- |
| `manifests` | Dependency manifests found at root or one level deep |
| `ecosystems` | Ecosystems inferred from manifests (`pypi`, `npm`, `packagist`, `crates`, `go`, `maven`, `rubygems`, `nuget`, `hex`) |

## `metrics`

See [metrics.md](metrics.md) for the full methodology. Structure:

```jsonc
{
  "metrics_version": "0.1.0",
  "overall":               { /* Metric */ },
  "activity":              { /* Metric */ },
  "maintainer_resilience": { /* Metric */ },
  "responsiveness":        { /* Metric */ },
  "community_health":      { /* Metric */ },
  "engineering_practices": { /* Metric */ },
  "security_posture":      { /* Metric */ }
}
```

Each metric object:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `key` | string | Stable machine identifier |
| `name` | string | Human-readable name |
| `value` | int | Score, always **1..100**, higher is better |
| `band` | string | Standardized interval: `critical` / `at_risk` / `moderate` / `good` / `excellent` |
| `inputs` | object | The raw data values the score was computed from (transparency/audit trail) |
| `note` | string? | Caveats, e.g. components excluded due to missing data |

A metric is `null` when none of its inputs could be collected — **missing
data is never silently scored**.
