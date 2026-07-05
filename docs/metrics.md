# Metrics methodology

**Metrics version: 0.2.0** (`metrics.metrics_version` in every report).
Formulas live in [`src/scanner/metrics.py`](../src/scanner/metrics.py); this
document is the human-readable specification. Any change to a formula, weight,
or band threshold bumps the metrics version. Transparency is the product:
every score can be recomputed by hand from the report's `data` section, and
every metric echoes its inputs in `inputs`.

Metrics are **signals, not warranties**. A high score means publicly visible
good practices; it is not a code audit and not a security guarantee.

## Standardized scale and bands

Every metric is an integer in **1..100**, higher is better. Values map to
five standardized bands, used consistently across health, safety, and quality
metrics:

| Band | Range | Meaning |
| ---- | ----- | ------- |
| `excellent` | 85–100 | Exemplary; meets essentially all checked criteria |
| `good` | 70–84 | Healthy; minor gaps |
| `moderate` | 50–69 | Acceptable with notable gaps; review recommended |
| `at_risk` | 30–49 | Significant weaknesses; adoption warrants caution |
| `critical` | 1–29 | Severe problems (e.g. abandoned, single-maintainer, no hygiene) |

Band thresholds are part of the versioned methodology.

## General scoring rules

1. Each metric is a weighted sum of **components**; component weights sum to 100.
2. If a component's underlying data is unavailable (`null` in `data`), the
   component is **excluded and the remaining weights renormalized** — missing
   data is never counted as zero. The metric's `note` says when this happened.
3. If *no* component has data, the metric is `null`.
4. Results are rounded and clamped to 1..100.
5. Every component is reported in the metric's `components` array with its
   earned/max points and a status — `met`, `partial`, `missed`, or `excluded`
   — so a report reader can see exactly which criteria passed. The value is
   always `round(100 × Σpoints / Σmax_points)` over non-excluded components.

### Version history

- **0.2.0** (2026-07-06) — per-component results (`components`) added to every
  metric. Formulas, weights and band thresholds unchanged from 0.1.0; scores
  are identical.
- **0.1.0** (2026-07-06) — initial methodology.

## Metric definitions

### `activity` — Development activity

*Is the project actively developed?*

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Push recency | 35 | days since last push: ≤7 → 35, ≤30 → 28, ≤90 → 18, ≤180 → 10, ≤365 → 4, else 0 |
| Commit cadence | 35 | `min(active_weeks_last_year, 52) / 52 × 35` |
| Commit volume | 15 | `min(15, log10(commits_last_year + 1) × 7.5)` (≈100 commits/yr saturates) |
| Release practice | 15 | mean gap ≤45 d → 15, ≤120 d → 10, releases exist → 6, no releases → 0 |

### `maintainer_resilience` — Bus factor

*Can the project survive losing its top maintainer?* `null` when the
contributor list is unavailable.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Bus factor | 60 | 1 → 10, 2 → 28, 3 → 40, 4 → 48, ≥5 → `min(60, 48 + (bf−4)×3)` |
| Distribution | 25 | `(1 − top_contributor_share) × 25` |
| Contributor breadth | 15 | `min(15, contributors_sampled × 1.5)` (10+ contributors saturates) |

### `responsiveness` — Issue & PR handling

*Are issues and pull requests actually being handled?* `null` when the repo
has no issues and no decided PRs.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Issue resolution | 55 | `issue_closed_ratio × 55` |
| PR acceptance | 45 | `merged / (merged + closed_unmerged) × 45` |

Known limitation (v0.1.0): counts are lifetime totals, not time-windowed, and
closing issues without fixing them still counts as "resolution". Latency
percentiles are planned.

### `community_health` — Community & documentation

*Is the project set up to receive users and contributors?* Checklist:

| Item | Weight |
| ---- | ------ |
| README | 25 |
| License | 20 |
| CONTRIBUTING | 15 |
| Code of conduct | 10 |
| Issue template | 10 |
| Docs directory | 10 |
| PR template | 5 |
| Repo description | 5 |

### `engineering_practices` — Engineering hygiene

*Baseline quality practices visible in the repository.* Checklist:

| Item | Weight |
| ---- | ------ |
| CI (GitHub Actions workflows) | 30 |
| Tests present | 30 |
| Linter configuration | 15 |
| Pre-commit hooks | 10 |
| Docs directory | 10 |
| .editorconfig | 5 |

### `security_posture` — Visible security hygiene

| Item | Weight | Note |
| ---- | ------ | ---- |
| Security policy (SECURITY.md) | 30 | |
| Dependabot configuration | 25 | |
| CodeQL workflow | 20 | |
| Dependency lockfiles | 25 | **Only scored when dependency manifests exist**; otherwise excluded and weights renormalized |

### `overall` — Overall health

Weighted mean of the available metrics (weights renormalized when a metric is
`null`; the `note` lists exclusions):

| Metric | Weight |
| ------ | ------ |
| activity | 0.20 |
| maintainer_resilience | 0.20 |
| security_posture | 0.15 |
| engineering_practices | 0.15 |
| responsiveness | 0.15 |
| community_health | 0.15 |

`overall.inputs` contains the per-metric values that went into the mean.

## Worked example

A repo pushed 2 days ago, active 31/52 weeks, 405 commits/yr, releases every
~8 days → activity components: 35 + 20.9 + 15 + 15 = 85.9 → **86, excellent**.

The same repo with `bus_factor = 1`, top contributor share 0.97, 4
contributors → 10 + 0.8 + 6 = 16.8 → **17, critical** — one metric can flag a
risk that others mask, which is why the report always shows the full vector,
not just `overall`.

## Roadmap (not yet scored)

- Issue/PR latency percentiles (time-windowed, not lifetime)
- Dependency freshness & known CVEs (ecosystem adapters)
- Popularity-normalized expectations (a 50-star repo ≠ a 50k-star repo)
- Test coverage and CI pass-rate signals
