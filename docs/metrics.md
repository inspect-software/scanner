# Metrics methodology

**Metrics version: 1.7.0** (`metrics.metrics_version` in every report).
Formulas live in [`src/scanner/metrics.py`](../src/scanner/metrics.py); this
document is the human-readable specification. Any change to a formula, weight,
or band threshold bumps the metrics version. Transparency is the product:
every score can be recomputed by hand from the report's `data` section, and
every metric echoes its inputs in `inputs`.

When bumping the version, the literal is scattered across five surfaces that
must move together: `src/scanner/metrics.py` (the `METRICS_VERSION` constant),
this file's header line **and** a new `Version history` entry,
`website/frontend/content/pages/methodology.md` (both the "currently vX" line
and the "history to X" line), and
`website/frontend/content/wiki/methodology-versions.md` (the "current version
is X" line plus a dated entry). Also update any wiki metric article whose
weights or component labels changed.

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

- A **metric** is normally a weighted sum of components (rule 1 above). Two
  policies are the documented exceptions: confirmed high-risk jurisdiction
  exposure multiplies `security_posture` and caps that metric at 49, and a
  confirmed inorganic growth finding discounts the stars and forks components
  of `popularity`.
- A **category** normally groups related metrics as a weighted mean of its
  available metrics (weights renormalized when a metric is `null`). A category
  with no scorable metric is dropped. Security is the documented exception:
  its jurisdiction risk signal is a penalty multiplier, so clean evidence can
  never raise weak security hygiene.
- The **overall** score begins as the weighted mean of the available
  categories. Confirmed high-risk jurisdiction exposure then applies the
  same multiplier and caps the result at **49 (`at_risk`)**.

Every category also carries its own `value`/`band` so a reader can compare
strengths across whole areas at a glance.

### Version history

- **1.7.0** (2026-07-21) — the Inorganic Growth Policy gains
  `star_concentration`, a repo-level corroborating signal: the five busiest
  days holding 80% or more of every collected star. Added after the first live
  evaluation, which found the policy precise but nearly blind — over 714
  ordinary repositories it flagged none, and over 45 selected for the shape
  purchased attention leaves it flagged one.

  The threshold is read off the record rather than chosen: across 502 ordinary
  repositories in the 100–1,500 star band the median puts 6.9% of stars in its
  five busiest days and **none** reached 80%. It corroborates rather than
  concludes, because a legitimate announcement-driven release does approach it
  — a research model drop measured 73.7% — and a signal that cannot tell those
  apart must not decide alone.

  This does not address slow, drip-fed acquisition, which produces no burst at
  all and so is invisible to every window-based signal. Measuring that needs
  the ordinary distribution of forks and watchers per star, which a sample
  selected on low fork counts cannot supply; a random baseline sample is in
  flight.
- **1.6.0** (2026-07-21) — inflated inputs stop counting at face value, in
  three places.

  `popularity` gains the **Inorganic Growth Policy**: a factor over its stars
  and forks components, derived from the per-day history already collected for
  the history chart. Stars are the one input in the model that is sold openly
  in bulk, and a report that treats a purchased star like an earned one is
  repeating a claim it never checked. A burst is confirmed only when two
  independent signals corroborate it — flat cadence, absent fork response,
  missing decay, or a spike predating anything released — which makes a launch,
  a Hacker News front page, or a newsletter mention read as `organic`.
  Confirmed windows discount stars and forks by 40% (`anomalous`) or 70%
  (`highly_anomalous`); watchers are untouched. Anything the collected window
  cannot answer is `unverified` and costs nothing, which is the common case:
  history collection is bounded, and quiet evidence is not suspicious evidence.
  No additive weight, so a clean history can never raise a score.

  Automation, meanwhile, stops counting as maintenance in two places.

  `development_activity` gains a **human-authorship factor** over its commit
  cadence and commit volume components — `min(1, human_share / 0.40)` across
  the newest 100 commits, with no additive weight of its own, exactly as the
  jurisdiction multiplier works. Component weights are unchanged. A project
  sustained by its robots no longer reads as alive: `caolan/async` has 28,000
  stars, is unarchived, was pushed within the year, and has not had a human
  commit since 2024-09-02 while Dependabot authored 80 of its newest 100; it
  loses 7 points. Full marks at a 40% human share and above, so heavy but
  genuine automation is untouched — of eight large projects measured, async was
  the only one whose score moved at all. The factor is 1.0 whenever the sample
  is missing or under 20 commits, so no scan is punished for data it could not
  collect.

  Separately, **AI Readiness** stops measuring only which files exist.
  `ai_agent_context` gains **Legible commit history** (40) and rebalances
  `llms.txt` from 40 to 15 and agent instructions from 60 to 45 — before this,
  six of eight large projects scored the floor value of 1 and the whole sample
  produced two distinct values, because the metric could only ask whether a
  rare file had been written. `ai_verify_loop` gains its first two outcome
  signals — **Demonstrated agent practice** (10) and **Automated maintenance**
  (8) — and now credits toolchain manifests as a one-command bootstrap, which
  had scored Rust projects zero for shipping no Makefile while `cargo test` is
  the ecosystem's canonical verify loop; its file-presence weights rescale to
  make room (22.5/27/13.5/13.5/13.5 → 18/22/11/11/10), leaving the Scorecard
  component's ×1 mapping intact.

  The two outcome signals are scored apart because either can appear without
  the other: one says a coding agent's work lands here, the other that a
  dependency bot's does. Both read the same commit sample that
  `development_activity` uses to *discount* bot commits — the same fact,
  answering two different questions. The category still carries weight 0.0, so
  none of this reaches the health score.

  `maintainer_resilience` now counts people only.
  Automation accounts were previously treated as maintainers, which flattered
  exactly the projects that automate most: in prettier, Renovate and Dependabot
  ranked second and third among all contributors, and removing them moves the
  bus factor from 3 to 2 and the top-contributor share from 27% to 43%. In
  vuejs/core the share moves from 55% to 67%. Projects that use no bots are
  unaffected (django, kubernetes: unchanged). Scores can only fall, never rise.
  Detection uses GitHub's `Bot` account type plus the reserved `[bot]` login
  suffix; a bot running as an ordinary user account is still counted as a
  person, so this corrects a bias rather than eliminating it.
- **1.5.0** (2026-07-20) — added `dependency_advisories`: the resolved
  dependency set collected from GitHub's dependency-graph SBOM is now matched
  against **OSV.dev** advisories. For repositories that publish a package the
  assessed set is that package's runtime closure, resolved from deps.dev —
  what installing it pulls in — falling back to the repository graph
  otherwise; `advisories.scope` records which. Security becomes a weighted mean of
  `security_posture` (0.8) and `dependency_advisories` (0.2); the jurisdiction
  multiplier still applies to posture and to the weighted overall score, and
  carries no additive weight of its own. The new metric is `null` — excluded
  and renormalized, per the ordinary missing-data rule — whenever the
  dependency graph or the lookup was unavailable, so no repository is
  penalized for having GitHub's dependency graph disabled. Costs one batch
  request per 1000 packages against a free unauthenticated API and no GitHub
  API budget.

  Scored on three components — direct, indirect, and advisories left
  outstanding past 90 days — with per-finding severity taken from the
  advisory's **CVSS base score** rather than the coarse database label, and
  each component computed as *worst-finding ceiling × hyperbolic volume* so
  one critical outweighs many trivial findings and large counts stay ordered
  instead of collapsing to zero.
- **1.4.0** (2026-07-19) — added `high_risk_jurisdiction_exposure`, an offline
  high-risk jurisdiction exposure signal for the Russia, Iran, and North
  Korea policy scope. It classifies
  self-published owner, displayed top-contributor, and their public
  organization-profile locations with a compact GeoNames-derived gazetteer.
  Only high-confidence country/region/place evidence scores; conflicting or
  ambiguous locations are review-only. A confirmed match applies its
  multiplier to `security_posture`, caps Security at 49, then applies the same
  multiplier to the weighted overall score and caps overall at 49: 20% for an
  owner match, 50% for a top contributor, and 75% for a public organization
  affiliation. Missing location data is excluded.
  This is a supply-chain policy signal, not nationality, citizenship,
  sanctions status, or malicious intent.

- **1.3.0** (2026-07-18) — the community-health license signal became an
  explicit three-state tier: recognized SPDX license (full credit), custom
  license (75%), or absent (0%). All available license sources are resolved
  together so a file seen by any source counts as present.

- **1.2.0** (2026-07-14) — published-package registry adapters added for
  **Go** (the module proxy), **Maven Central**, and **NuGet**, and PyPI
  identification extended to legacy `setup.py`. Repos publishing in those
  ecosystems now score `package_maintenance` (publish recency, version
  history, deprecation); NuGet also feeds `ecosystem_adoption` through its
  lifetime download total. Go and Maven publish no download statistics, so
  they add no adoption signal. No formulas changed — only which repos have
  registry evidence. See [ecosystems.md](ecosystems.md).

- **1.1.0** (2026-07-14) — `community_health` now detects a license **once**.
  The former separate `OpenSSF Scorecard: License` card (weight 10) is gone; the
  metric's single `License` component (weight 22.5) is sourced from Scorecard's
  graded `License` check and falls back to GitHub's community-profile license
  flag only when Scorecard is unavailable or inconclusive. The component is
  labelled generically as `License` — Scorecard's own `License` check remains a
  full component of `security_posture`. This removes double-counting and lets
  Scorecard's more reliable detection settle disagreements (in production the
  GitHub flag and Scorecard disagreed both ways; Scorecard was the accurate
  side).

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

- **0.9.0** (2026-07-07) — security-posture **fallback** no longer penalizes
  published libraries for omitting a dependency lockfile. Committing a lockfile
  is an application concern; libraries/gems (e.g. Ruby gems) conventionally do
  not, so the check is now excluded and renormalized for repos that publish a
  package — only applications (dependencies declared, nothing published) are
  scored on it. Affects only the file-signal fallback; the OpenSSF Scorecard
  path is unchanged.
- **0.8.0** (2026-06-30) — **AI Readiness** category added: four metrics
  (`ai_agent_context`, `ai_verify_loop`, `ai_code_legibility`, `ai_interfaces`)
  scoring how well a repo is set up for AI coding agents. The category carries
  weight **0.0** — an independent, additive badge that never changes the overall
  health score. All pre-existing formulas and scores are unchanged.
- **0.7.0** (2026-06-20) — supported ecosystems extended. `ecosystem_adoption`
  falls back to lifetime `total_downloads` when a registry publishes no monthly
  figure (RubyGems), so Ruby and Hex packages now score on adoption. RubyGems
  and Hex registry adapters added; declared-dependency parsing extended to Go,
  Maven, RubyGems, NuGet, and Hex. Only the `ecosystem_adoption` formula
  changed; all other formulas unchanged. See [ecosystems.md](ecosystems.md).
- **0.6.0** (2026-06-09) — `security_posture` rebuilt on **OpenSSF Scorecard**
  (via the `scorecard` CLI): tool-agnostic, risk-weighted checks that no longer
  penalize projects for using non-GitHub tooling, with inconclusive checks
  excluded rather than scored zero. Coarse file-tree checks remain as a fallback
  when the CLI is unavailable. Only the Security category is affected; all other
  formulas unchanged.
- **0.5.0** (2026-05-29) — package-ecosystem metrics added: `ecosystem_adoption`
  (registry downloads) in Community & Adoption and `package_maintenance`
  (registry publish recency / deprecation) in Sustainability & Governance. Both
  are `null` for repos that publish no package. Category inner weights
  rebalanced to make room; category weights and other formulas unchanged. See
  [ecosystems.md](ecosystems.md).
- **0.4.0** (2026-05-18) — metrics regrouped into five weighted **categories**
  with rolled-up scores. Four new repository metrics: `release_discipline`,
  `popularity`, `stewardship` (organization vs. personal-account backing), and
  `documentation`. `activity` renamed `development_activity` (release signals
  split out). Organization metrics regrouped into two categories. Overall now
  rolls up categories rather than individual metrics.
- **0.3.0** (2026-05-06) — organization metrics added (profile completeness,
  portfolio activity, community reach, org overall). Repository formulas
  unchanged; repository scores identical to 0.2.0.
- **0.2.0** (2026-04-25) — per-component results (`components`) added to every
  metric. Formulas, weights and band thresholds unchanged from 0.1.0; scores
  are identical.
- **0.1.0** (2026-04-15) — initial methodology.

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
| **Security** | 0.16 | security_posture (0.8), dependency_advisories (0.2), × High-Risk Jurisdiction Policy multiplier |
| **AI Readiness** | 0.00 | ai_agent_context (0.30), ai_verify_loop (0.40), ai_code_legibility (0.15), ai_interfaces (0.15) |

`ecosystem_adoption` and `package_maintenance` only apply to repos that
publish a package (see [ecosystems.md](ecosystems.md)); for everything else
they are `null`, excluded from their category with weights renormalized — so a
non-publishing repo is scored purely on its other metrics.

A metric's effective weight in the overall score is normally *category weight
× within-category weight*. Security's high-risk jurisdiction exposure is instead a
documented multiplier over the posture value and has no additive weight.

### Vitality

**`development_activity`** — *Is code actively being written?*

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Push recency | 36 | days since last push; same thresholds as v0.9.0, scaled to 36 points |
| Commit cadence | 36 | `min(active_weeks, 52) / 52 × 36`, × human-authorship factor |
| Commit volume | 18 | log-scaled, ~100 commits/yr saturates, × human-authorship factor |
| OpenSSF Scorecard: Maintained | 10 | Scorecard's 0–10 `Maintained` result × 1; excluded when unavailable or inconclusive |

**The human-authorship factor.** Cadence and volume read GitHub counters that
cannot tell a maintainer's work from a robot's, so a Dependabot bump counts as
development. The measured case is `caolan/async`: 28,000 stars, unarchived,
pushed within the year, and 80 of its newest 100 commits authored by
Dependabot — its last human commit dates to 2024-09-02. Undiscounted, such a
project reads as maintained.

The factor is `min(1, human_share / 0.40)` over the newest 100 commits, applied
to those two components only. Push recency reads a timestamp rather than a
commit count and is not inflated by automation, so it is not discounted.

It carries no additive weight of its own, following the high-risk jurisdiction
multiplier. That is deliberate and was established by measurement: an additive
"human authorship" component worth 15 points *raised* `caolan/async` by 4,
because a half-earned component lifts any metric already scoring below half. A
signal meant to catch inflation must deflate the inflated inputs rather than
sit beside them.

Full marks at a 40% human share and above — a floor test for human
involvement, not a preference for manual work. kubernetes runs 48% bot commits
and is plainly maintained, so the threshold sits below that. Measured across
eight large projects, only `caolan/async` was affected (−7 on the metric);
every other project scored identically with and without the factor.

The factor is **1.0** — no effect — whenever the sample is missing or smaller
than 20 commits: an unauthenticated scan, a GraphQL fallback to REST, or a young
repository must never lose points for data the scan did not collect.

The sample is the newest 100 commits, so the period it covers varies by orders
of magnitude between projects — 15 days for webpack, 4,286 for chalk/ansi-styles.
When the factor applies, the evidence line states the span it measured for
exactly this reason. Only automation with a GitHub App identity is recognized;
see the `maintainer_resilience` note on `k8s-ci-robot`.

**`release_discipline`** — *Does the project ship versioned releases?* `null`
when release data is unavailable.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| Ships releases | 27 | any published releases; same condition as v0.9.0, scaled to 27 points |
| Release recency | 36 | same thresholds as v0.9.0, scaled to 36 points |
| Release cadence | 27 | same thresholds as v0.9.0, scaled to 27 points |
| OpenSSF Scorecard: Signed-Releases | 10 | Scorecard's 0–10 `Signed-Releases` result × 1; excluded when unavailable or inconclusive |

### Community & Adoption

**`popularity`** — *How much adoption and attention?* (all log-scaled; counts of
1–2 earn nothing, scoring starts at 3)

| Component | Weight | Saturates at |
| --------- | ------ | ------------ |
| Stars | 60 | ~5,000 |
| Forks | 25 | ~1,000 |
| Watchers | 15 | ~500 |

Stars and forks additionally carry a **growth-authenticity factor** — the
Inorganic Growth Policy, detailed below. It has no additive weight of its own.

#### Inorganic Growth Policy

Stars are the most widely read trust signal in open source and the only one
with no issuer: they are sold in bulk, publicly, for a few cents each. The
per-day star and fork history collected for the history chart is read for
bursts that organic attention does not produce, and where one is confirmed the
two purchasable components — stars and forks — are discounted. Watchers are
untouched; they are not part of what the anomaly evidences. See
[`growth.py`](../src/scanner/growth.py) for the constants.

A burst alone is never a finding. A window of spike days (a day clearing both
25 stars and 12× the repository's own median active-day rate) is **confirmed
only when at least two independent signals corroborate it**:

| Signal | Fires when |
| ------ | ---------- |
| `star_concentration` | The five busiest days hold 80% or more of every star collected — repo-level, so it corroborates every window; requires `complete` history |
| `flat_cadence` | ≥3 active days whose daily counts vary by a coefficient under 0.25 — a delivery schedule, not an audience |
| `fork_divergence` | The window's fork-to-star ratio is under a quarter of the repository's own long-run ratio |
| `missing_decay` | The following 7 days total ≤5% of the burst — real spikes have a tail as the link circulates |
| `pre_substance_spike` | The burst predates anything the project had released |

| State | Confirmed windows | Factor |
| ----- | ----------------- | ------ |
| `organic` | none | 1.00 |
| `unverified` | not assessable | 1.00 |
| `anomalous` | one, with two signals | 0.60 |
| `highly_anomalous` | two or more, or one with three signals | 0.30 |

`unverified` carries **no penalty**: it covers a repository with no collected
history, under 100 stars, or a collected window spanning under 60 days. History
collection is bounded (see [report-schema.md](report-schema.md)), so a quiet or
truncated window is the ordinary case, not a suspicious one — and manipulation
older than the window is invisible to this policy, which is a limit of the
evidence, not a clean result. Every signal is a statement about the timing of
public events; none establishes that attention was purchased or that the
maintainers were involved.

**`community_health`** — *Set up to receive users and contributors?* Checklist:
README (22.5), License (22.5), CONTRIBUTING guide (18), Code of conduct
(13.5), Issue template (7.2), PR template (6.3). The License component is
detected by OpenSSF Scorecard's `License` check (its 0–10 result × 2.25) and
falls back to GitHub's community-profile license flag when Scorecard is
unavailable; it is shown simply as `License`.

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
`null` when the contributor list is unavailable, and equally when every
contributor turned out to be automation.

All three contributor components count **people only**: accounts GitHub types
as `Bot`, or whose login carries the reserved `[bot]` suffix, are removed
before the bus factor, the distribution, or the breadth is derived. They are
counted in `data.maintainership.bot_contributors` so the exclusion is visible.
Bots running under ordinary user accounts cannot be told apart from people
here and are still counted as contributors — kubernetes' top contributor,
`k8s-ci-robot`, has 27,325 commits and the type `User`.

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

Security combines two additive metrics, then applies the policy multiplier:

`Security = weighted_mean(security_posture 0.8, dependency_advisories 0.2)`

with `security_posture` already carrying the jurisdiction adjustment:

`security_posture = round(security_posture × high_risk_jurisdiction_exposure / 100)`

`dependency_advisories` is `null` — excluded, with the remaining weight
renormalized — whenever neither the published package's runtime closure nor the
repository dependency graph could be resolved, so Security then scores on
posture alone.

The multiplier cannot improve the posture score, and a confirmed match caps
the adjusted posture at 49 (`at_risk`). The Security category mirrors that
adjusted posture without multiplying it twice. The weighted overall score then
receives the same multiplier and 49 cap. If no relevant profile location is
available, the jurisdiction metric is `null` and none of these adjustments run.

**`dependency_advisories`** — *Do the dependencies a consumer installs carry
known advisories?* Matched against **OSV.dev**, a free, unauthenticated advisory
API aggregating GHSA, PYSEC, RUSTSEC and the rest. One batch request per 1000
packages; no GitHub API budget is consumed.

**What is assessed depends on what the repository publishes**, and the report
records which in `data.dependencies.advisories.scope`:

- `published_package` — the **runtime closure of the published package**,
  resolved from [deps.dev](https://deps.dev) in one call
  ([`runtime_deps.py`](../src/scanner/runtime_deps.py)). This is what
  installing the package actually pulls in, and it is the preferred source.
- `repository_graph` — the repository dependency graph from
  [`sbom.py`](../src/scanner/sbom.py), used when the repository publishes
  nothing the index resolves. It includes development and test pins that never
  ship, so findings in this scope may concern tooling rather than software.

deps.dev resolves dependency *graphs* for **npm, PyPI, crates.io and Maven**
only; its `:dependencies` endpoint returns 404 for Go, NuGet, RubyGems,
Packagist and Hex (verified 2026-07-20 against known-good packages). Those
ecosystems therefore always use `repository_graph`, silently — the limitation
is the index's, not the repository's, so it produces no warning.

The distinction is not cosmetic. `pallets/flask` assessed against its
repository graph reports 6 affected packages among 106 — largely Sphinx,
pytest and old test-matrix pins. Assessed against published `Flask`, the
closure is 6 packages and none carries an advisory. Only the second answers
what a consumer is exposed to.

Three components:

| Component | Weight |
|---|---:|
| Direct dependencies free of known advisories | 35 |
| Indirect dependencies free of known advisories | 25 |
| No advisories left outstanding | 40 |

**Severity comes from CVSS, not a label.** Each affected package contributes
`penalty = cvss_base_score / 10` in 0..1, computed from the advisory's
published v3.x or v4.0 vector. The coarse database label is the fallback only
where no usable vector exists — in sampling it was present on 76% of records
against CVSS's 95%, so it is both rarer and less precise.

**Each of the first two components is `ceiling × volume`**, not a plain sum:

```
worst   = max(penalty)                       # the single worst finding
ceiling = 1 − 0.8 × worst                    # what the worst exposure leaves
volume  = 1 / (1 + (Σ penalty − worst) / 4)  # marginal harm of the rest
points  = max_points × ceiling × volume
```

Two properties that a summed-severity decay lacked. The worst finding
dominates: one CVSS 9.8 costs 78% of a component, where eight CVSS 2.0
findings cost 38% — previously a hundred trivial advisories could outweigh a
critical. And the volume term is hyperbolic, so the score keeps falling
without ever reaching zero: 10, 50 and 300 critical findings score 1.7, 0.4
and 0.1 of 25 rather than collapsing to an indistinguishable zero past five.

**The third component is a maintenance signal, not a security one.** Being hit
by an advisory published last week is bad luck; still resolving to a version
affected by one published a year ago is a failure to track dependencies. A
package counts as outstanding once its oldest advisory is more than
**90 days** old, and the component earns `max_points / (1 + Σ penalty / 4)`
over those. Publication date stands in for fix availability — OSV carries it
on every record. The component is *excluded and renormalized* when no finding
has a date, never assumed fresh.

Packages whose version the dependency graph did not record cannot be matched
against a version range: they are **skipped and counted** in
`unassessed_count`, and the metric note states the coverage.

Deliberately **separate from `security_posture`** rather than another component
of it. Scorecard's own `Vulnerabilities` check already queries OSV and already
contributes to posture at High risk weight; folding a second OSV-derived signal
into the same metric would count one body of evidence twice. The two answer
different questions — Scorecard asks whether the project carries
known-vulnerable dependencies at all, this asks which ones, how severe, and
fixed in which version.

**Limits, stated in the report rather than hidden.** An advisory here means a
resolved version falls in an advisory's affected range. It is not a
reachability or exploitability finding: most advisories in a large closure are
not exploitable in context. In `repository_graph` scope the development and
test contamination described above applies, and the metric note says so.

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
- Seven checks also substantiate other health metrics where they are
  semantically relevant. Six appear there as modest, additive **shared
  evidence** cards (`OpenSSF Scorecard: <check>`); the seventh, `License`, is
  instead `community_health`'s primary license signal, shown generically as
  `License`. Security remains the complete,
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

**`high_risk_jurisdiction_exposure`** — *Does public profile evidence trigger
the High-Risk Jurisdiction Policy?* The scanner evaluates only
self-published GitHub profile locations already collected for the repository
owner, displayed top contributors, and public organizations shown on those
profiles. It performs no nationality inference and makes no additional GitHub
request.

| Strongest high-confidence evidence | Multiplier | Security effect |
| --- | ---: | --- |
| Repository owner in Russia, Iran, or North Korea | 20 | base Security reduced by 80% |
| Displayed top contributor | 50 | base Security reduced by 50% |
| Contributor's public organization affiliation | 75 | base Security reduced by 25% |
| Assessed location(s), no target-country match | 100 | no change |
| No assessable location | `null` | metric excluded; no change |

Any of the first three confirmed matches applies the same multiplier and cap at
both policy-sensitive hierarchy points: `security_posture` and the weighted
**overall repository score**. Reports record the base, multiplier, multiplied
value, and cap in each affected metric's `inputs`.

Country names, native spellings, flags, unambiguous administrative regions,
and unique or strongly dominant place names are high-confidence evidence.
Another country or US state in the same free-text location downgrades the match
to review-only. Internationally recognized Ukrainian territories are excluded
from Russia place matching unless the profile explicitly self-declares Russia.
The runtime gazetteer is generated from the free
[GeoNames dump](https://download.geonames.org/export/dump/) (CC BY 4.0) and is
packaged with the scanner; it makes no network call.

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
| Agent instructions | 45 | CLAUDE.md / AGENTS.md / `.cursor/rules` / Copilot instructions / GEMINI.md / …; a file below ~200 bytes scores partial (stub) |
| Machine-readable docs (llms.txt) | 15 | `llms.txt` / `llms-full.txt` present |
| Legible commit history | 40 | share of **human** commits stating their intent; full marks at ≥75% |

**Legible commit history** asks whether the record the project already produces
carries intent — what an agent consults before editing unfamiliar code. A
commit counts when its subject is structured (conventional-commit form, or a
reference to the issue or PR behind it) *or* its body explains the change.

Both forms count because either alone is parochial, which measurement
confirmed: requiring structure scored the Linux kernel at 18% despite its
exemplary explanatory messages, while requiring bodies would have failed
vuejs/core at 2% despite a fully conventional history. Counting either, both
land near 99% and 100%.

Bot commits are excluded — automated subjects are uniformly well-formed and
would evidence nothing about the project's own practice. The component is
excluded when fewer than 20 human commits were sampled.

Tracker-key references of the general `ABC-123` form are deliberately *not*
recognized: across twelve large projects the pattern matched six subjects, one
of them the false positive "WTF-16", and it would equally have matched
"UTF-8", "SHA-256" and "ISO-8601".

The rebalance away from `llms.txt` is deliberate. It is a documentation-
consumption standard aimed at AI readers of a project's docs, not at agents
editing its code, and it is rare enough that at weight 40 it dominated a
metric most projects could not move at all: measured before this change, six
of eight large projects scored the floor value of 1, and only two values were
observed across the whole sample.

**`ai_verify_loop`** — *Can an agent set up, run, and verify a change on its
own?* The crux for autonomous agents, hence the heaviest weight in the category.

| Component | Weight | Scoring |
| --------- | ------ | ------- |
| One-command bootstrap | 18 | Makefile / Taskfile / justfile / mise / noxfile → full; a toolchain manifest that defines the command itself (`Cargo.toml`, `go.mod`, `mix.exs`, Maven/Gradle, `*.csproj`) → 12.6 |
| Automated tests | 22 | a test suite the agent can run to self-check (reuses the engineering test signal) |
| Lint / format config | 11 | reuses the engineering linter signal |
| Static type checking | 11 | a statically typed language, or a type-check config (mypy / pyright / tsconfig / `py.typed`) |
| Reproducible environment | 10 | devcontainer / Dockerfile / Nix / dependency lockfile |
| Demonstrated agent practice | 10 | share of sampled commits authored or co-authored by a coding agent; full marks at ≥5% |
| Automated maintenance | 8 | dependency-update bot commits observed in the sample → full; a `dependabot.yml` with nothing observed → 5 |
| OpenSSF Scorecard: Pinned-Dependencies | 10 | Scorecard's 0–10 result × 1; excluded when unavailable or inconclusive |

Crediting only a task runner taxed whole ecosystems for having better
defaults: `cargo test` and `go test ./...` are the canonical verify loops of
their languages, yet rust-lang/regex and serde scored zero on bootstrap.
Toolchain manifests now earn most of the credit, a task runner still earns all
of it. `package.json` and `pyproject.toml` are excluded on purpose — npm and
Python define no universal test command, and whether the project defines one
lives in file contents this scan does not read.

**Demonstrated agent practice** is the only outcome signal in the category.
Every other component reads the file tree: a task runner exists, a lockfile
exists — proxies for a loop nobody has observed running. Commits an agent
authored, or that a maintainer credited an agent for, say the loop was closed
at least once in practice. Detected via `bots.py`, from the same commit sample
as the other commit-based signals.

**Automated maintenance** is the second outcome signal, and deliberately a
separate one: a dependency bot's pull request is a machine-authored change that
must clear the same gates an agent's would — the tests run, the checks pass, it
merges. A project already absorbing that traffic has demonstrated the pathway an
agent needs. Either signal can be present without the other, so they are scored
apart rather than merged.

Observed commits outrank configuration, because a `dependabot.yml` can sit in a
repository with the integration switched off, and Renovate is routinely
configured outside the repository altogether: measured, prettier and vuejs/core
run it across a third of their commits while carrying no recognizable config
file, and would score zero on a configuration-only test. Only bots that author
their own content count — kubernetes-prow[bot] wrote 50 of kubernetes' newest
100 commits but merges other people's work, and is excluded.

Note the deliberate asymmetry with `development_activity`, where these same bot
commits *discount* the score. The metrics ask different questions: Vitality asks
whether people still maintain this, AI Readiness asks whether machines can. A
repository can honestly answer yes to one and no to the other, and the same fact
is evidence for both answers.

It evidences *adoption, not autonomy*: a maintainer driving an agent
interactively leaves the same trailer as an unattended run, and public data
cannot separate them — hence the modest weight and the deliberately factual
name. Agent detection is a floor, so absence is read as absence of evidence,
never as proof the loop is broken. The component is excluded, not zeroed, when
no commit sample was collected. Measured across the newest 100 commits:
gin 11, react 13, axios 6, prettier 5, requests 1, and zero elsewhere.

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
