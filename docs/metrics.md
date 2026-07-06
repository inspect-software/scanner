# Metrics methodology

**Metrics version: 0.4.0** (`metrics.metrics_version` in every report).
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

### Three-level hierarchy

Metrics roll up into a transparent hierarchy: **components → metrics →
categories → overall**.

- A **metric** is a weighted sum of components (rule 1 above).
- A **category** groups related metrics; its score is the weighted mean of its
  available metrics (weights renormalized when a metric is `null`). A category
  with no scorable metric is dropped.
- The **overall** score is the weighted mean of the available categories.

Every category also carries its own `value`/`band` so a reader can compare
strengths across whole areas at a glance.

### Version history

- **0.4.0** (2026-07-06) — metrics regrouped into five weighted **categories**
  with rolled-up scores. Four new repository metrics: `release_discipline`,
  `popularity`, `stewardship` (organization vs. personal-account backing), and
  `documentation`. `activity` renamed `development_activity` (release signals
  split out). Organization metrics regrouped into two categories. Overall now
  rolls up categories rather than individual metrics.
- **0.3.0** (2026-07-06) — organization metrics added (profile completeness,
  portfolio activity, community reach, org overall). Repository formulas
  unchanged; repository scores identical to 0.2.0.
- **0.2.0** (2026-07-06) — per-component results (`components`) added to every
  metric. Formulas, weights and band thresholds unchanged from 0.1.0; scores
  are identical.
- **0.1.0** (2026-07-06) — initial methodology.

## Repository categories & metrics

Ten metrics in five categories. Category weights sum to 1.0; within a
category the metric weights also sum to 1.0.

| Category | Weight | Metrics (weight within category) |
| -------- | ------ | -------------------------------- |
| **Vitality** | 0.22 | development_activity (0.6), release_discipline (0.4) |
| **Community & Adoption** | 0.18 | popularity (0.5), community_health (0.5) |
| **Sustainability & Governance** | 0.24 | maintainer_resilience (0.4), responsiveness (0.3), stewardship (0.3) |
| **Engineering Quality** | 0.20 | engineering_practices (0.6), documentation (0.4) |
| **Security** | 0.16 | security_posture (1.0) |

A metric's effective weight in the overall score is *category weight ×
within-category weight* (shown on each metric card as "Weight in overall
score").

### Vitality

**`development_activity`** — *Is code actively being written?*

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Push recency | 40 | days since last push: ≤7 → 40, ≤30 → 32, ≤90 → 20, ≤180 → 11, ≤365 → 4, else 0 |
| Commit cadence | 40 | `min(active_weeks, 52) / 52 × 40` |
| Commit volume | 20 | log-scaled, ~100 commits/yr saturates |

**`release_discipline`** — *Does the project ship versioned releases?* `null`
when release data is unavailable.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Ships releases | 30 | any published releases → 30, else 0 |
| Release recency | 40 | latest ≤90 d → 40, ≤180 → 30, ≤365 → 18, ≤730 → 8, else 0 |
| Release cadence | 30 | mean gap ≤45 d → 30, ≤120 → 22, ≤365 → 14, else 6 |

### Community & Adoption

**`popularity`** — *How much adoption and attention?* (all log-scaled)

| Component | Weight | Saturates at |
| --------- | ------ | ------------ |
| Stars | 60 | ~5,000 |
| Forks | 25 | ~1,000 |
| Watchers | 15 | ~500 |

**`community_health`** — *Set up to receive users and contributors?* Checklist:
README (25), License (25), CONTRIBUTING guide (20), Code of conduct (15),
Issue template (8), PR template (7).

### Sustainability & Governance

**`maintainer_resilience`** — *Can it survive losing its top maintainer?*
`null` when the contributor list is unavailable.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Bus factor | 60 | 1 → 10, 2 → 28, 3 → 40, 4 → 48, ≥5 → `min(60, 48 + (bf−4)×3)` |
| Commit distribution | 25 | `(1 − top_contributor_share) × 25` |
| Contributor breadth | 15 | `min(15, contributors_sampled × 1.5)` |

**`responsiveness`** — *Are issues and PRs handled?* `null` with no issues and
no decided PRs. Issue resolution (55): `issue_closed_ratio × 55`. PR acceptance
(45): `merged / (merged + closed_unmerged) × 45`. Counts are lifetime totals;
latency percentiles are planned.

**`stewardship`** — *Who stands behind this repo?* `null` when the owner
profile is unavailable. **This is where organization backing influences the
score** — an organization signals shared, accountable stewardship that can
outlive any single person.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Ownership backing | 30 | Organization → 30, personal (User) account → 10 |
| Verified domain | 20 | org with verified domain → 20, org without → 0; **excluded for user accounts** |
| Owner reach | 25 | followers of the owning account, log-scaled (~3,000 saturates) |
| Track record | 25 | account age (≥6 yr → 12) + public repos (log-scaled → 13) |

So a repository moved from a personal account to an organization gains up to
20 points of backing plus access to the verified-domain component — a
deliberate, transparent lift for organization-stewarded projects.

### Engineering Quality

**`engineering_practices`** — *Baseline engineering hygiene?* Checklist:
CI workflows (30), Tests present (30), Linter config (20), Pre-commit hooks
(12), .editorconfig (8).

**`documentation`** — *Can a newcomer learn what it is and how to use it?*
Checklist: README (30), Documentation directory (25), Documentation/homepage
site (15), Repository description (10), Topics (10), Wiki (10).

### Security

**`security_posture`** — *Visible security hygiene?*

| Component | Weight | Note |
| --------- | ------ | ---- |
| Security policy (SECURITY.md) | 30 | |
| Dependabot configuration | 25 | |
| Dependency lockfiles | 25 | **Only scored when dependency manifests exist**; otherwise excluded and renormalized |
| CodeQL workflow | 20 | |

## Organization categories & metrics

Organizations use the same 1..100 scale and bands, in two categories.
Portfolio facts are computed over a sample of up to 100 public repos (API
page cap), most recently pushed first.

| Category | Weight | Metrics (weight within category) |
| -------- | ------ | -------------------------------- |
| **Activity & Reach** | 0.75 | portfolio_activity (0.6), community_reach (0.4) |
| **Governance & Profile** | 0.25 | profile_completeness (1.0) |

**`portfolio_activity`** — Recently active repos (50, share pushed in last 90 d),
Yearly active repos (25, share pushed in last year), Portfolio size (15,
log-scaled ~100 repos), Original work (10, non-fork share).

**`community_reach`** — Followers (50, log-scaled ~1,000), Stars across
repositories (50, log-scaled ~10,000).

**`profile_completeness`** — Verified domain (25), Description (20), Homepage
(15), Display name (10), Location (10), Contact email (10), Social profile (10).

Repository reports also embed the owning account's public profile in
`data.owner` (both organizations and users); it feeds the `stewardship`
metric above.

## Worked example

pallets/flask (organization-owned): Vitality 75, Community & Adoption 96,
Sustainability & Governance 70, Engineering Quality 96, Security 25 → weighted
by category → **overall 74, good**. Its `stewardship` scores 75 (org backing +
2,347 followers). The *same repository* under a personal account with the same
following would lose the 20-point organization-backing margin and the
verified-domain component — dropping stewardship into the moderate band and
pulling the Governance category down with it. That is the ownership influence,
made explicit and auditable.

## Roadmap (not yet scored)

- Issue/PR latency percentiles (time-windowed, not lifetime)
- Dependency freshness & known CVEs (ecosystem adapters)
- Popularity-normalized expectations (a 50-star repo ≠ a 50k-star repo)
- Test coverage and CI pass-rate signals
