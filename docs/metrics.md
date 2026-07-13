# Metrics methodology

**Metrics version: 1.0.0** (`metrics.metrics_version` in every report).
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

- **1.0.0** (2026-07-13) — selected OpenSSF Scorecard checks now provide
  **shared evidence** to the health dimensions they also describe, while
  remaining full components of `security_posture`: `Maintained` → development
  activity; `Signed-Releases` → release discipline; `Contributors` →
  maintainer resilience; `Code-Review` → responsiveness; `License` → community
  health; `CI-Tests` → engineering practices; and `Pinned-Dependencies` → AI
  verify loop. These category-specific components total 10–20 points; they do
  not reuse Scorecard's security risk weights. An unavailable or inconclusive
  check is excluded and remaining components renormalize. This intentionally
  lets a practice affect every health dimension it substantiates.

- **0.9.0** (2026-07-06) — security-posture **fallback** no longer penalizes
  published libraries for omitting a dependency lockfile. Committing a lockfile
  is an application concern; libraries/gems (e.g. Ruby gems) conventionally do
  not, so the check is now excluded and renormalized for repos that publish a
  package — only applications (dependencies declared, nothing published) are
  scored on it. Affects only the file-signal fallback; the OpenSSF Scorecard
  path is unchanged.
- **0.8.0** (2026-07-06) — **AI Readiness** category added: four metrics
  (`ai_agent_context`, `ai_verify_loop`, `ai_code_legibility`, `ai_interfaces`)
  scoring how well a repo is set up for AI coding agents. The category carries
  weight **0.0** — an independent, additive badge that never changes the overall
  health score. All pre-existing formulas and scores are unchanged.
- **0.7.0** (2026-07-06) — supported ecosystems extended. `ecosystem_adoption`
  falls back to lifetime `total_downloads` when a registry publishes no monthly
  figure (RubyGems), so Ruby and Hex packages now score on adoption. RubyGems
  and Hex registry adapters added; declared-dependency parsing extended to Go,
  Maven, RubyGems, NuGet, and Hex. Only the `ecosystem_adoption` formula
  changed; all other formulas unchanged. See [ecosystems.md](ecosystems.md).
- **0.6.0** (2026-07-06) — `security_posture` rebuilt on **OpenSSF Scorecard**
  (via the `scorecard` CLI): tool-agnostic, risk-weighted checks that no longer
  penalize projects for using non-GitHub tooling, with inconclusive checks
  excluded rather than scored zero. Coarse file-tree checks remain as a fallback
  when the CLI is unavailable. Only the Security category is affected; all other
  formulas unchanged.
- **0.5.0** (2026-07-06) — package-ecosystem metrics added: `ecosystem_adoption`
  (registry downloads) in Community & Adoption and `package_maintenance`
  (registry publish recency / deprecation) in Sustainability & Governance. Both
  are `null` for repos that publish no package. Category inner weights
  rebalanced to make room; category weights and other formulas unchanged. See
  [ecosystems.md](ecosystems.md).
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

The five **scored** categories carry weights that sum to 1.0; within a category
the metric weights also sum to 1.0. A sixth category, **AI Readiness**, is an
independent badge with weight **0.0** — it is computed and shown but never
affects the overall health score.

| Category | Weight | Metrics (weight within category) |
| -------- | ------ | -------------------------------- |
| **Vitality** | 0.22 | development_activity (0.6), release_discipline (0.4) |
| **Community & Adoption** | 0.18 | popularity (0.4), community_health (0.35), ecosystem_adoption (0.25) |
| **Sustainability & Governance** | 0.24 | maintainer_resilience (0.3), responsiveness (0.25), stewardship (0.25), package_maintenance (0.2) |
| **Engineering Quality** | 0.20 | engineering_practices (0.6), documentation (0.4) |
| **Security** | 0.16 | security_posture (1.0) |
| **AI Readiness** | 0.00 | ai_agent_context (0.30), ai_verify_loop (0.40), ai_code_legibility (0.15), ai_interfaces (0.15) |

`ecosystem_adoption` and `package_maintenance` only apply to repos that
publish a package (see [ecosystems.md](ecosystems.md)); for everything else
they are `null`, excluded from their category with weights renormalized — so a
non-publishing repo is scored purely on its other metrics.

A metric's effective weight in the overall score is *category weight ×
within-category weight* (shown on each metric card as "Weight in overall
score").

### Vitality

**`development_activity`** — *Is code actively being written?*

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Push recency | 36 | days since last push; same thresholds as v0.9.0, scaled to 36 points |
| Commit cadence | 36 | `min(active_weeks, 52) / 52 × 36` |
| Commit volume | 18 | log-scaled, ~100 commits/yr saturates |
| OpenSSF Scorecard: Maintained | 10 | Scorecard's 0–10 `Maintained` result × 1; excluded when unavailable or inconclusive |

**`release_discipline`** — *Does the project ship versioned releases?* `null`
when release data is unavailable.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Ships releases | 27 | any published releases; same condition as v0.9.0, scaled to 27 points |
| Release recency | 36 | same thresholds as v0.9.0, scaled to 36 points |
| Release cadence | 27 | same thresholds as v0.9.0, scaled to 27 points |
| OpenSSF Scorecard: Signed-Releases | 10 | Scorecard's 0–10 `Signed-Releases` result × 1; excluded when unavailable or inconclusive |

### Community & Adoption

**`popularity`** — *How much adoption and attention?* (all log-scaled)

| Component | Weight | Saturates at |
| --------- | ------ | ------------ |
| Stars | 60 | ~5,000 |
| Forks | 25 | ~1,000 |
| Watchers | 15 | ~500 |

**`community_health`** — *Set up to receive users and contributors?* Checklist:
README (22.5), License (22.5), CONTRIBUTING guide (18), Code of conduct
(13.5), Issue template (7.2), PR template (6.3), and OpenSSF Scorecard:
License (10; its 0–10 result × 1).

**`ecosystem_adoption`** — *How widely is the package actually installed?*
`null` for repos that publish no package or when no download data is available.
Real registry downloads beat GitHub stars as an adoption signal.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Monthly downloads | 80 | summed across the repo's packages, log-scaled (~1,000,000/mo saturates) |
| Total downloads | 80 | fallback when no monthly figure is published (e.g. RubyGems), log-scaled (~50,000,000 all-time saturates) |
| Registry dependents | 20 | log-scaled; reported by some ecosystems only, else excluded |

The download component is monthly where available, otherwise lifetime total —
whichever the registry provides (see [ecosystems.md](ecosystems.md)).

### Sustainability & Governance

**`maintainer_resilience`** — *Can it survive losing its top maintainer?*
`null` when the contributor list is unavailable.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Bus factor | 54 | v0.9.0 bus-factor curve, scaled to 54 points |
| Commit distribution | 22.5 | `(1 − top_contributor_share) × 22.5` |
| Contributor breadth | 13.5 | `min(13.5, contributors_sampled × 1.35)` |
| OpenSSF Scorecard: Contributors | 10 | Scorecard's 0–10 `Contributors` result × 1; excluded when unavailable or inconclusive |

**`responsiveness`** — *Are issues and PRs handled?* `null` with no issues and
no decided PRs. Issue resolution (46.75): `issue_closed_ratio × 46.75`. PR acceptance
(38.25): `merged / (merged + closed_unmerged) × 38.25`. OpenSSF Scorecard:
Code-Review (15) is its 0–10 result × 1.5. Counts are lifetime totals; latency
percentiles are planned.

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

**`package_maintenance`** — *Is the published package current and not
deprecated?* `null` for repos that publish no package. Registry upkeep is
distinct from GitHub activity — a library can go stale or be marked abandoned
on the registry while its repo still sees commits.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Published & resolvable | 25 | ≥1 of the repo's packages resolves on its registry |
| Publish recency | 35 | latest publish ≤180 d → 35, ≤365 → 26, ≤730 → 14, else 4 |
| Version history | 20 | ≥5 versions → 20, ≥2 → 12, else 4 |
| Not deprecated | 20 | deprecated (npm) / abandoned (Packagist) / yanked-latest → 0, else 20 |

### Engineering Quality

**`engineering_practices`** — *Baseline engineering hygiene?* Checklist:
CI workflows (24), Tests present (24), Linter config (16), Pre-commit hooks
(9.6), .editorconfig (6.4), and OpenSSF Scorecard: CI-Tests (20; its 0–10
result × 2).

**`documentation`** — *Can a newcomer learn what it is and how to use it?*
Checklist: README (30), Documentation directory (25), Documentation/homepage
site (15), Repository description (10), Topics (10), Wiki (10).

### Security

**`security_posture`** — *Visible security hygiene?* Backed by **OpenSSF
Scorecard** (https://github.com/ossf/scorecard), a neutral, versioned,
**tool-agnostic** security standard. We deliberately do *not* score the
presence of one vendor's config file — Scorecard's checks reward the *practice*:
any accepted dependency-update tool (Dependabot **or** Renovate **or** …), any
SAST (CodeQL **or** Semgrep **or** …), signed releases, least-privilege workflow
tokens, no known-vulnerable dependencies, and more.

- Each Scorecard check becomes a component, **weighted by Scorecard's own risk
  level** (Critical 10 / High 7.5 / Medium 5 / Low 2.5), so the rolled-up 1..100
  value tracks Scorecard's 0–10 aggregate (`value ≈ aggregate × 10`).
- A check Scorecard reports as **inconclusive** (`-1`, e.g. Branch-Protection
  without an admin token) is **excluded and renormalized** — never scored as a
  zero. This is the key fix for well-run projects that previously scored near
  zero because they used non-GitHub tooling or exposed no admin-only signals.
- The full per-check breakdown (score, reason, docs link) is in the report's
  `data.security_signals.scorecard` and rendered as a dedicated section.
- Seven checks also contribute modest **shared evidence** to other health
  metrics where they are semantically relevant. Security remains the complete,
  risk-weighted Scorecard result; the other metrics use their own documented
  category-specific weights.

Running Scorecard needs the `scorecard` CLI on `PATH`; the scan resolves the
same GitHub token it already uses. When the CLI is unavailable, disabled, or
fails, the metric **falls back** to coarse file-tree signals (`inputs.source ==
"file_signals"`):

| Fallback component | Weight | Note |
| ------------------ | ------ | ---- |
| Security policy (SECURITY.md) | 30 | |
| Dependabot configuration | 25 | |
| Dependency lockfiles | 25 | Scored only for **applications** — repos that declare dependencies but publish no package. Excluded and renormalized when there are no dependency manifests, or when the repo publishes a library/gem (which by convention does not commit a lockfile — e.g. Ruby gems, so absence is not a fault) |
| CodeQL workflow | 20 | |

### AI Readiness

How well the repo is equipped to be **developed and maintained with AI coding
agents**. This is an **independent, additive badge**: the category carries
weight **0.0**, so it is computed and displayed on its own but never changes the
overall health score — a solid pre-AI-era project is not marked down for lacking
agent tooling. Signals are **presence- and size-based** heuristics from the file
tree (no file contents): they show the infrastructure exists, not how good it
is. Substance is weighted where cheap to detect (a stub instruction file scores
partial), to resist gaming.

**`ai_agent_context`** — *Does it give agents guidance and machine-readable docs?*

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Agent instructions | 60 | CLAUDE.md / AGENTS.md / `.cursor/rules` / Copilot instructions / GEMINI.md / …; a file below ~200 bytes scores partial (stub) |
| Machine-readable docs (llms.txt) | 40 | `llms.txt` / `llms-full.txt` present |

**`ai_verify_loop`** — *Can an agent set up, run, and verify a change on its
own?* The crux for autonomous agents, hence the heaviest weight in the category.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| One-command bootstrap | 22.5 | Makefile / Taskfile / justfile / mise / noxfile |
| Automated tests | 27 | a test suite the agent can run to self-check (reuses the engineering test signal) |
| Lint / format config | 13.5 | reuses the engineering linter signal |
| Static type checking | 13.5 | a statically typed language, or a type-check config (mypy / pyright / tsconfig / `py.typed`) |
| Reproducible environment | 13.5 | devcontainer / Dockerfile / Nix / dependency lockfile |
| OpenSSF Scorecard: Pinned-Dependencies | 10 | Scorecard's 0–10 result × 1; excluded when unavailable or inconclusive |

**`ai_code_legibility`** — *Is the code legible to a model?* `null` for repos
with no detectable source files (so docs-only repos are not penalized).

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Type-checkable code | 45 | statically typed language → 45; dynamically typed with a type-check config → 27; else 0 |
| Manageable file sizes | 55 | `(1 − oversized/total) × 55`, where a source file over ~60 KB (~1,500 lines) is "oversized"; vendored/generated paths excluded |

**`ai_interfaces`** — *Does it expose machine-readable interfaces?* `null` when
the repo exposes **none** of these — a plain library legitimately has no API
schema, so absence is treated as not-applicable, never as a penalty.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| API schema (OpenAPI/GraphQL/proto) | 40 | OpenAPI/Swagger, GraphQL SDL, protobuf, or AsyncAPI files |
| MCP server | 20 | a Model Context Protocol server dependency or `mcp.json` config |
| Runnable examples | 40 | `examples/` · `recipes/` · `samples/` directories, or notebooks |

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

## Configuration (enabling / disabling metrics)

A scan can switch off any part of the methodology — a **component**, a whole
**metric**, or a whole **category**. This is carried by a `ScanConfig` and
**embedded in every report** (`config` at the top level), so a score is always
reproducible and it is explicit what was and was not measured.

Disabling something works *exactly like missing data*: the item is excluded and
the remaining weights are **renormalized**, so the score stays on the 1..100
scale — it is never counted as a zero. Disabling every metric in a category (or
the category itself) drops that category from the overall, with its weight
renormalized away.

```jsonc
// scan-config.json
{
  "disabled_categories": ["security"],          // drop a whole category
  "disabled_metrics": ["popularity"],           // drop one metric
  "disabled_components": {                        // drop components within a metric
    "documentation": ["Wiki", "Topics"]
  }
}
```

From the CLI, a config file and/or repeatable flags (flags merge on top of the
file):

```
inspect-scan owner/repo --config scan-config.json
inspect-scan owner/repo --disable-category security --disable-metric popularity
inspect-scan owner/repo --disable-component documentation:Wiki --html report.html
```

Category and metric **keys** are the identifiers in the tables above
(`security`, `popularity`, `security_posture`, …); component **names** are the
exact display names (`Wiki`, `Stars`, `README`, …). Unknown category/metric
keys are reported as warnings and ignored. The HTML report renders a **Scan
configuration** section listing what was disabled (or "Full methodology" when
nothing is), and each affected metric's `note` records the renormalization.

Configuration selects *which* parts of the fixed methodology are active; it does
not change any formula, weight, or threshold — those remain versioned by
`metrics_version`.

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
- Dependency freshness & known CVEs (ecosystem adapters) — the declared
  dependency list itself is now collected (`data.dependencies.dependencies`,
  see [ecosystems.md](ecosystems.md#declared-dependencies)); resolving it
  against the registry and vulnerability databases is the remaining work
- Popularity-normalized expectations (a 50-star repo ≠ a 50k-star repo)
- Test coverage and CI pass-rate signals
