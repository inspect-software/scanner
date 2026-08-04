# Report schema

**Schema version: 0.30.0** (`schema_version` field in every report).
The schema is defined as Pydantic models in
[`src/scanner/models.py`](../src/scanner/models.py); this document describes
it for consumers. Any breaking structural change bumps `schema_version`.

## Recent schema changes

No field was removed or renamed, and a report produced under an older version
simply lacks the newer ones. The one change a consumer must actually handle is
0.28.0: `band` carries two values it never carried before.

| Version | Change |
| ------- | ------ |
| **0.31.0** | `data.icon` ([`IconInfo`](#dataicon--the-mark-that-identifies-the-repository)) — which image identifies the repository, which of five sources it came from, and what was rejected on the way |
| **0.30.0** | `data.artifacts` ([`ArtifactSignals`](#dataartifacts--what-the-repository-builds)) — what the repository's manifests and file tree say it builds, as canonical tokens; `metrics.classification` ([`Classification`](#metricsclassification--how-the-software-is-consumed)) — the labels derived from them. `ecosystem.packages[].declared_type` and `.categories` — artifact type and controlled-vocabulary categories, where a registry publishes them |
| **0.29.0** | `contribution_flow.recent_prs` ([`RecentPullRequests`](#datacontribution_flowrecent_prs)) — windowed and first-time-contributor pull-request outcomes, feeding the `Newcomer PR acceptance` component added in metrics 2.1.0. `community.readme_badges` ([`ReadmeBadges`](#datacommunityreadme_badges)) — the README's status badges, carried unscored |
| **0.28.0** | The band scale went from five values to seven: `weak` between `at_risk` and `moderate`, `exceptional` above `excellent`. Every `band` field in `metrics` can now carry the two new values (see metrics 2.0.0) |
| **0.27.0** | `popularity.star_history.collected_at` — the UTC day a star history was captured, needed because a history collected before GitHub restricted the `stargazers` connection is carried forward into later scans rather than recollected |

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
  "schema_version": "0.30.0",
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

### `data.contacts` — maintainer contact channels (not published)

Ways to reach the people responsible for the project, gathered from sources
the scan already reads: the owner's GitHub profile, the repository's
`SECURITY.md`, and the registry entries of the packages it publishes.

**This field is withheld from published reports.** It is present in reports the
scanner writes locally (`-o report.json`), and it is stored in the website's
database — but the website's `/api/repositories/{owner}/{name}/report`
endpoint strips it (`PUBLIC_REPORT_EXCLUDE` in `website/backend/app/main.py`),
and the generated HTML report omits it from its raw-JSON card. Every value is
self-published by the maintainer upstream, but it is personal data, and the
report endpoint is unauthenticated; serving it would make the site a
harvesting surface for maintainer addresses.

Nothing here is scored — contacts exist to reach maintainers, not to judge
them. A project that publishes no contact details is not penalized.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `kind` | string | `email`, `url`, or `handle` |
| `value` | string | The address, URL, or `@handle`, verbatim as published |
| `role` | string | `owner`, `author`, `maintainer`, `security`, `funding`, `issues`, `chat`, `support` |
| `source` | string | `github-owner-profile`, `pypi`, `npm`, `rubygems`, `hex`, `packagist`, `crates`, or the `SECURITY.md` path |

```jsonc
"contacts": [
  { "kind": "handle", "value": "@ThePSF", "role": "owner",
    "source": "github-owner-profile" },
  { "kind": "email", "value": "python-maint@redhat.com", "role": "security",
    "source": ".github/SECURITY.md" },
  { "kind": "email", "value": "me@kennethreitz.org", "role": "author",
    "source": "pypi" }
]
```

Collection is conservative by design:

- Unroutable addresses are dropped — GitHub `noreply` forwarders, `no-reply@`
  localparts, and `example.com`-style placeholder domains.
- Registry entries pointing at a *different* repository are excluded, along
  with their contacts: those maintainers are not this project's.
- Links count only when their label names a channel (funding, issues, chat,
  support); a `Documentation` URL is not a contact.
- `SECURITY.md` is fetched only when the file tree says it exists, so it costs
  one request on repos with a policy and none on those without. An unreadable
  policy is a warning, never a scan failure.
- Go, Maven, and NuGet publish no usable addresses on their public endpoints
  and contribute nothing here.

### `data.owner` — owning account profile (repository reports)

Public profile of the account that owns the repository — **organization or
user**. `null` only when the profile could not be fetched.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `login`, `type` | string | Account login and `"User"` / `"Organization"` |
| `name`, `company`, `blog`, `location` | string? | Self-published profile fields |
| `followers`, `public_repos` | int | Reach and portfolio size |
| `created_at`, `account_age_days` | | Account track record |
| `is_verified` | bool? | Verified-domain badge (organizations only; `null` for users) |
| `avatar_url` | string? | |

Unlike other `data`, this one **does** feed a score: the `stewardship` metric
(see metrics.md) reads it to reward organization backing. The facts themselves
are still just observations.

The same GitHub payload carries the owner's published `email` and
`twitter_username`. Those are deliberately *not* fields here — they are
contact data, and land in [`data.contacts`](#datacontacts--maintainer-contact-channels-not-published)
instead, which is withheld from published reports.

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
| `significant_languages` | string[] | Languages holding ≥10% of total bytes, largest first (capped at 5) — the languages the repo is written in, with CI/script residue filtered out. Falls back to `[primary_language]` when the byte map is unavailable |
| `topics` | string[] | Repository topics |
| `license_spdx` | string? | SPDX id of the detected license (`null` if none/unrecognized) |

### `data.popularity`

| Field | Description |
| ----- | ----------- |
| `stars`, `forks` | Standard GitHub counters |
| `watchers` | Subscribers (notification watchers, not the legacy stars alias) |
| `open_issues_and_prs` | GitHub's combined open issues + open PRs counter |
| `star_history` | Per-day star additions for the stars-over-time chart (object, below). `null` when unavailable — no token, the GraphQL fetch failed, or the repo exceeds the popularity ceiling |
| `fork_history` | Per-day fork additions for the forks-over-time chart (object, below). `null` when unavailable — no token, the GraphQL fetch failed, or the repo exceeds the popularity ceiling |

#### `data.popularity.star_history`

Star timestamps collected newest-first from GitHub's GraphQL `stargazers`
connection and bucketed by UTC day. Bounded: at most 10 pages (1000 star
events) are fetched, and repositories above 100,000 stars are skipped (deep
pagination is slow and the captured window would be too short to chart).

| Field | Description |
| ----- | ----------- |
| `total_stars` | The repository's current star count |
| `collected` | Star events actually fetched (< `total_stars` when truncated) |
| `complete` | `true` when the whole history fit inside the page cap. When `false`, `days` is a recent window only, and a cumulative curve must be anchored at `total_stars` (working backwards), not at zero |
| `days` | `[{ "date": "YYYY-MM-DD", "count": N }]`, ascending — stars added per UTC day |
| `collected_at` | UTC day the history was captured, ISO `YYYY-MM-DD`; `null` on histories captured before schema 0.27.0 |

GitHub restricted the `stargazers` connection to a repository's own admins and
collaborators in July 2026, so a history that was collected can never be
collected again. A previously collected history is therefore carried forward
into later scans rather than overwritten with `null`, and it keeps the
`total_stars` it was captured with — re-anchoring it to today's star count
would invent growth nobody observed. `collected_at` is consequently older than
the report's own `generated_at` whenever a history was carried forward, and any
consumer overlaying several series has to read it to know where observation
ends.

#### `data.popularity.fork_history`

Fork timestamps collected newest-first from GitHub's GraphQL `forks` connection
(each fork node's `createdAt`) and bucketed by UTC day. Bounded exactly like
`star_history`: at most 10 pages (1000 fork events) are fetched, and
repositories above 100,000 forks are skipped.

| Field | Description |
| ----- | ----------- |
| `total_forks` | The repository's current fork count |
| `collected` | Fork events actually fetched (< `total_forks` when truncated) |
| `complete` | `true` when the whole history fit inside the page cap. When `false`, `days` is a recent window only, and a cumulative curve must be anchored at `total_forks` (working backwards), not at zero |
| `days` | `[{ "date": "YYYY-MM-DD", "count": N }]`, ascending — forks added per UTC day |

### `data.activity`

| Field | Description |
| ----- | ----------- |
| `recent_commits` | Up to 100 newest commits on the default branch, newest first (array, below). Empty when unavailable — no token, an empty repository, or the GraphQL snapshot fell back to REST |
| `commits_last_year` | Total commits in the last 52 weeks (all contributors) |
| `active_weeks_last_year` | Weeks with ≥1 commit in the last 52 |
| `days_since_last_push` | Days since the last push to any branch |
| `releases_count` | Releases fetched (capped at 100); semver tags when the repo has no GitHub Releases |
| `releases_from_tags` | True when the counts above come from semver git tags rather than GitHub Releases |
| `releases` | Up to 100 most recent releases, newest first (array, below) — the release timeline |
| `latest_release_tag`, `latest_release_at` | Most recent release (or newest semver tag) |
| `mean_days_between_releases` | Mean gap between the most recent releases (up to 10) |

#### `data.activity.releases[]`

The same releases the aggregates above are derived from, kept as a list so the
repository page can plot a release timeline. They ride along on the snapshot
query that is made anyway, so the list costs no additional request.

| Field | Description |
| ----- | ----------- |
| `tag` | Version tag as published, e.g. `v2.1.0` |
| `published_at` | Publication date, or `null` when the tag carries no resolved date |
| `kind` | `major`, `minor`, `patch`, `prerelease`, or `other` |

`kind` is read from the tag alone rather than by diffing consecutive versions:
the fetched window is capped at 100, so the release preceding a given one is
not always present, and a diff-based rule would mislabel the oldest release in
the window. `X.0.0` is major, `X.Y.0` minor, any other three-part version a
patch; a `-rc`/`-beta` suffix makes it a prerelease (a `+build` suffix does
not), and a tag that is not semver at all is `other`.

#### `data.activity.recent_commits[]`

Commit messages as their authors wrote them — not GitHub commit *comments*,
which are a separate, web-only thing this report does not collect. Fetched by
the same GraphQL query that carries the rest of the repository snapshot (its
`defaultBranchRef` history connection), so they cost no additional request and
no additional rate-limit point. There is no REST equivalent in the fallback
path: an unauthenticated scan reports an empty array.

| Field | Description |
| ----- | ----------- |
| `oid` | Full commit SHA |
| `committed_at` | Committer date (ISO 8601) |
| `headline` | First line of the commit message |
| `body` | Everything after the first line; `null` for single-line messages. Long bodies keep their first 300 and last 200 characters with `[…]` between — the middle is elided, never the tail, because git trailers (`Co-authored-by`, `Signed-off-by`, the `Generated with` lines coding agents append) are the last thing in a message and identify who or what wrote the commit |
| `body_truncated` | `true` when the middle of `body` was elided |
| `author_login` | Author's GitHub login; `null` when the commit's email maps to no account |
| `author_name` | Author name recorded in the git commit itself |
| `is_bot` | The authoring *account* is automation (a GitHub App: Dependabot, Renovate, a CI bot), not a person |
| `is_coding_agent` | An LLM coding agent *wrote the change* — either committing under its own account or credited in a `Co-authored-by` trailer |

The two flags are independent, not a two-value enum. A human who commits work
produced with Claude Code or Cursor is `is_bot: false`, `is_coding_agent: true`
— that combination is the common one, and on measured samples it is the
majority of agent activity. A Dependabot bump is `true`/`false`. A Copilot
coding-agent commit is `true`/`true`. Ordinary human work is `false`/`false`.

Detection ([`bots.py`](../src/scanner/bots.py)) uses identifiers GitHub
controls — the reserved `[bot]` login suffix, verified agent app logins, and
agent trailer addresses — never a loose keyword match, so a repository that
merely *discusses* these tools is not flagged. Note that the GraphQL commit
history cannot answer the bot question directly: `author.user.__typename`
returns `User` even for `renovate[bot]`, though REST `/users/{login}` reports
`"type": "Bot"` for the same account.

The agent list is inevitably incomplete — new agents ship constantly. Detection
fails toward "human", so treat these flags as a floor on automation, not a
census. Measured across the newest 100 commits of ten large projects:
143/1000 commits were bot-authored (0 in django, flask, linux and rust;
48 in kubernetes) and 24/1000 were agent-written (13 of them in react, by core
maintainers using Claude Code and Cursor).

### `data.contribution_flow` — is arriving work still being acted on?

Where `data.activity` measures what maintainers *emit*, this measures what
they *answer*, and the difference is what separates a finished project from an
abandoned one: a complete library emits nothing and owes nothing, while an
abandoned one emits nothing while requests pile up against it. It feeds the
Abandonment Policy (see [metrics.md](metrics.md#abandonment-policy)); the
facts themselves are plain observations.

Carried by the same GraphQL snapshot as the rest of the repository metadata,
so it costs no additional request. `collected` is `false` on unauthenticated
scans and the REST fallback — which is **not** the same statement as an empty
tracker, and the difference decides whether anything may be derived from it.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `collected` | bool | The GraphQL snapshot supplied these fields |
| `last_merged_pr_at` | datetime? | Most recent merge among the 10 most recently updated merged pull requests; `null` when none has ever been merged. GraphQL cannot order pull requests by merge date, hence the sample |
| `oldest_open_prs` | list | Up to 20 longest-open pull requests, oldest first (below) |
| `oldest_open_issues` | list | Up to 20 longest-open issues, oldest first (below) |
| `ci_last_run_at` | datetime? | When CI last reported on the default branch head; `null` when the repository runs no checks |
| `ci_last_conclusion` | string? | Conclusion of that run (`SUCCESS`, `FAILURE`, …), verbatim from GitHub |
| `recent_prs` | object | Windowed pull-request outcomes (`RecentPullRequests`, below); every field stays empty when `collected` is `false` |

Both queues are sampled **oldest-first**. What is being established is not
what a tracker is busy with but what has been sitting in it unanswered, and
that is at the far end of the queue, not the near one.

| `TrackedItem` | Type | Description |
| ----- | ---- | ----------- |
| `number` | int | Issue or pull-request number |
| `created_at` | datetime | When it was opened |
| `last_comment_at` | datetime? | Most recent comment; `null` when nobody has replied at all |
| `last_comment_author` | string? | Login of the most recent commenter — read to tell a maintainer's reply from the author talking to themselves; `null` when there is no comment, or the account was deleted |

#### `data.contribution_flow.recent_prs`

`maintainership.issues.merged_prs` already carries the all-time acceptance
rate, and on an established project that figure is close to immovable: a
repository with thousands of merged pull requests cannot move it inside a year
whatever it does now. These fields answer the same question over a window a
maintainer can still move, which is the question anyone deciding whether to
open a pull request is actually asking.

The `newcomer_*` fields narrow it further, to authors with **no previously
merged pull request in this repository**. A project can be quick and generous
with its regulars and merge nothing arriving from outside that circle; the two
rates come apart often enough that only the second describes what a first-time
contributor should expect. They feed the `Newcomer PR acceptance` component of
`responsiveness` (metrics 2.1.0).

Derived from a fixed-size sample of the most recently updated **decided** pull
requests (`snapshot.MAX_DECIDED_PR_SAMPLE`, currently 60), plus one batched
GraphQL probe of those authors' prior merge history.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `window_days` | int | Length of the long window every `*_30d` field covers (30) |
| `sample_size` | int | Decided pull requests read, across every window |
| `sample_exhausted` | bool | The sample ended inside the window, so the counts are **lower bounds** and only the `*_30d` ratios stay meaningful |
| `decided_7d`, `merged_7d` | int? | Decided and merged pull requests in the last 7 days |
| `decided_30d`, `merged_30d` | int? | The same over the long window |
| `authors_30d` | int? | Distinct human authors of decided pull requests in the window |
| `authors_probed_30d` | int? | How many of those authors' histories were actually checked. Below `authors_30d` when the probe cap was hit, and every `newcomer_*` figure then describes the probed subset only |
| `newcomer_authors_30d` | int? | Of the probed authors, those with no previously merged pull request here |
| `newcomer_decided_30d`, `newcomer_merged_30d` | int? | Their decided and merged pull requests in the window |
| `bot_prs_excluded_30d` | int | Automation-authored pull requests dropped before anything above was counted — a Dependabot queue merging itself is not contribution flow |

The seven-day figures are carried because they are free to compute, but at that
length the median repository decides nothing at all and the ratio is noise;
only the 30-day newcomer figures are scored. The whole block is populated only
on scans run after schema 0.29.0 — earlier reports lack it, and the metric
component renormalizes rather than scoring a zero.

### `data.maintainership`

| Field | Description |
| ----- | ----------- |
| `contributors_sampled` | **Human** contributors counted (top 100 by commits; anonymous and automation excluded) |
| `bot_contributors` | Automation accounts removed from every figure in this section. A floor: bots running under ordinary user accounts (kubernetes' `k8s-ci-robot`, 27,325 commits, type `User`) are indistinguishable from people here |
| `top_contributors` | Up to 10 entries. `login`, `commits`, `type`, and `avatar_url` come from the contributors endpoint. Authenticated scans may also store a `profile` containing self-published `name`, `location`, `company`, and up to 20 public `{login, name, location}` organization memberships, fetched for all displayed contributors in one GraphQL request. Profiles are withheld from public API/HTML reports; only aggregate, identity-free jurisdiction evidence may feed `high_risk_jurisdiction_exposure`. |
| `bus_factor` | Smallest number of contributors covering ≥50% of sampled commits |
| `top_contributor_share` | Share of sampled commits by the top contributor (0..1) |
| `issues.open_issues`, `issues.closed_issues` | Issue counts (search API) |
| `issues.closed_ratio` | closed / (open + closed); `null` if the repo has no issues |
| `issues.open_prs`, `issues.merged_prs`, `issues.closed_unmerged_prs` | PR counts |

### `data.community` — GitHub community profile

`health_percentage` plus boolean presence flags: `has_readme`, `has_license`,
`has_contributing`, `has_code_of_conduct`, `has_issue_template`,
`has_pull_request_template`, `has_description`.

#### `data.community.readme_badges`

The status badges the README displays — the strip of small SVG images most
READMEs open with (build status, coverage, package version, license,
downloads). Detection is by image URL, never by alt text, across the Markdown
and HTML image forms and reStructuredText's directive for `README.rst`.

**Descriptive only, and deliberately unscored.** Every fact a badge asserts is
already measured directly from the repository, so scoring the picture of the
fact alongside the fact would double-count it — and a badge is one line of
Markdown that nothing verifies as pointing at this project. The counts are
carried so a reader can see them; they carry no weight.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `collected` | bool | README markup was read; `false` leaves every count at zero |
| `total` | int | Distinct badge images anywhere in the README |
| `header` | int | Of those, the ones above the first section heading |
| `hosts` | string[] | Badge services used, sorted and deduplicated |
| `has_inspect_badge` | bool | Whether this project's own badge is among them — the only reliable way to tell badge *adoption* from badge *publication*, since the website records only that it pushed an SVG |

The recognized-host list is inevitably incomplete: new badge services appear
and projects self-host SVGs. A missed badge reads as no badge, which is the
safe direction to fail for a figure that is descriptive only.

### `data.quality_signals` — file-tree heuristics

| Field | Detection |
| ----- | --------- |
| `has_ci`, `ci_workflows` | `.github/workflows/*.yml\|yaml` |
| `has_tests` | `test(s)`/`spec(s)`/`__tests__` directories or test-file naming patterns |
| `has_docs_dir` | Non-empty `doc/`, `docs/`, `documentation/`, or `wiki/` directory |
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
| `dependencies` | **Direct** dependencies as declared, parsed straight from manifest text (see below) |
| `all_dependencies` | **Full resolved set** (direct + indirect/transitive) from the GitHub dependency-graph SBOM (see below) |
| `advisories` | Known advisories affecting the resolved set, matched against OSV.dev (see below) |

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

`all_dependencies` is the full **resolved** dependency set — direct plus
indirect/transitive — from GitHub's dependency-graph SBOM export. Collection
is best-effort and time-boxed (5 minutes): failures never abort a scan; they
set `error` and add a report warning. See
[ecosystems.md](ecosystems.md#all-dependencies-resolved-graph).

| Field | Type | Description |
| ----- | ---- | ----------- |
| `collected` | bool | The resolved graph was retrieved successfully |
| `source` | string? | `"github-sbom"` when collected; `null` otherwise |
| `error` | string? | Why collection failed/was skipped (also mirrored in `warnings`); `null` on success |
| `total_count` | int? | Resolved packages in the graph — always complete, even when the list is truncated |
| `direct_count` | int? | Resolved packages matching a declared direct runtime dependency |
| `indirect_count` | int? | Everything else: transitive dependencies (plus direct dev/test dependencies, which the declared list excludes) |
| `truncated` | bool | The embedded `packages` list was capped (2,000) to keep reports bounded |
| `packages` | list | `ResolvedDependency` objects: `ecosystem`, `name`, `version?`, `direct` — direct entries first |

`advisories` matches a resolved dependency set against
[OSV.dev](https://osv.dev) — a free, unauthenticated advisory API. One batch
request per 1,000 packages; no GitHub API budget is consumed. Best-effort and
time-boxed (2 minutes): on failure `collected` stays false, `error` says why,
and the `dependency_advisories` metric is excluded rather than scored zero.

**Which set is assessed** depends on what the repository publishes, and
`scope` records it. When the repository publishes a package the index
resolves, the assessed set is that package's **runtime closure** from
[deps.dev](https://deps.dev) — what installing it actually pulls in.
Otherwise the repository dependency graph is used, which also contains
development and test pins that never ship.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `collected` | bool | The advisory lookup completed |
| `source` | string? | `"osv"` when collected; `null` otherwise |
| `scope` | string? | `"published_package"` (runtime closure of the published package) or `"repository_graph"` (the repository's own graph, dev/test pins included) |
| `assessed_package` | string? | `"ecosystem:name@version"` in `published_package` scope; `null` otherwise |
| `error` | string? | Why the lookup failed/was skipped (also mirrored in `warnings`) |
| `assessed_count` | int | Resolved packages actually queried (version and supported ecosystem known) |
| `unassessed_count` | int | Resolved packages skipped — no version recorded, or an ecosystem OSV does not cover |
| `affected_count` | int | Assessed packages carrying at least one advisory |
| `direct_affected_count` | int | Affected packages that are declared direct runtime dependencies |
| `advisory_count` | int | Total advisories across affected packages |
| `by_severity` | object | Affected package counts keyed by worst severity (`critical`/`high`/`moderate`/`low`/`unknown`) |
| `truncated` | bool | The embedded `findings` list was capped (250); counts remain complete |
| `findings` | list | `AdvisoryFinding` objects, most severe first |
| `malicious_count` | int | Assessed packages reported as malicious packages |
| `malicious` | list | `MaliciousDependency` objects, direct entries first |

| `AdvisoryFinding` | Type | Description |
| ----- | ---- | ----------- |
| `ecosystem`, `name`, `version` | string | The affected package as resolved |
| `direct` | bool | Matches a declared direct runtime dependency |
| `severity` | string | Worst severity across its advisories, from the CVSS band where a vector is published, else the database label; `unknown` when neither exists |
| `cvss_score` | float? | Highest CVSS base score across its advisories, computed from the published v3.x/v4.0 vector; `null` when none carries one |
| `oldest_advisory_days` | int? | Days since its earliest advisory was published — how long a fix has been available and unapplied |
| `advisory_count` | int | Distinct advisories affecting this version |
| `advisory_ids` | list | Advisory identifiers (GHSA/PYSEC/…), capped at 10 |
| `fixed_version` | string? | Highest version an advisory records as fixed; `null` when none is stated |

An entry means the version recorded in the dependency graph falls in an
advisory's affected range. It is **not** a reachability or exploitability
finding, and GitHub's SBOM export does not distinguish development and test
pins from runtime dependencies — so a finding may concern tooling rather than
shipped software. `direct` is the closest available proxy.

| `MaliciousDependency` | Type | Description |
| ----- | ---- | ----------- |
| `ecosystem`, `name`, `version` | string | The package as resolved in the graph |
| `direct` | bool | Matches a declared direct runtime dependency; scored identically either way |
| `advisory_ids` | list | Malicious-package report identifiers (`MAL-…`/`GHSA-…`), capped at 10 |
| `first_reported_at` | datetime? | Earliest publication date across the reports; `null` when no record states one |
| `still_published` | bool? | Whether the registry still serves this exact version. `false` means the artifact has been pulled and the finding is reported without being scored; `null` means the check did not run or the ecosystem is not covered, and is treated as `true`. Probed for npm, PyPI, crates.io, RubyGems, Hex, Go and Maven Central — the registries answering a per-version URL by status. NuGet and Packagist answer with a version *list* instead and are left unprobed, so they always report `null` |

OSV.dev ingests the OpenSSF
[`ossf/malicious-packages`](https://github.com/ossf/malicious-packages) corpus
and serves it under `MAL-` identifiers, so these arrive on the same batch query
as ordinary advisories at no extra cost. They are **excluded from `findings`
and from every advisory count above**: a malicious package carries no CVSS
vector, no severity band, and no fixed version, and scoring it as an advisory
would rate it `unknown` severity — the weight of a moderate CVE. A package
carrying both kinds of record appears only here.

An entry concerns the package **as published**, not the maintainers of the
scanned repository, which may have resolved it unknowingly. The remedy is
removal, or moving off the compromised name — never an upgrade to a fixed
release of the same artifact, because there is none.

`still_published` asks about the **exact resolved version**, deliberately not
about the package's `latest`. npm's convention after a takedown is to leave an
`x.y.z-security` holding package as `latest`, which protects everyone resolving
a range and nobody who pinned the bad version. Reading `latest` would clear
repositories that still fetch the malicious artifact on every install — the
first live finding, `svaarala/duktape`, pins `http` at exactly `0.0.0`.

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
| `keywords` | string[] | Tags/keywords/categories/classifiers the registry lists (PyPI classifiers+keywords, npm keywords, Packagist keywords, crates.io keywords+categories, NuGet tags); empty where the registry has no such concept or none were declared |
| `declared_type` | string? | The artifact type the registry publishes itself, verbatim: Packagist `type` (`library`, `project`, `wordpress-plugin`, …), a NuGet package type (`DotnetTool`, `Template`), Maven `packaging` (`jar`/`war`/`pom`/`maven-plugin`). `null` where the registry declares none — most do not |
| `categories` | string[] | Registry categories from a *controlled* vocabulary, kept apart from free-form `keywords` because they are reliable enough to classify on. Only crates.io publishes one today (`command-line-utilities`, `web-programming::http-server`, `api-bindings`, …) |

This section **feeds** the `ecosystem_adoption` and `package_maintenance`
metrics; the facts themselves remain plain observations.

### `data.artifacts` — what the repository builds

What the manifests declare and the file tree shows about the *artifact*, as
opposed to the project's health. Recorded as canonical tokens rather than raw
manifest text, so the report stays small and the vocabulary stays stable across
manifest formats. Nothing here is interpreted: the mapping from tokens to
labels lives in [`metrics.classification`](#metricsclassification--how-the-software-is-consumed),
which means a rule correction reclassifies stored reports without a rescan.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `collected` | bool | The artifact scan ran. `false` on reports written before schema 0.30.0 — which is not the same as a repository that declares nothing |
| `declarations[]` | object[] | One entry per manifest that declares something: `path`, `ecosystem`, `name` (the package name the manifest declares, where it does), and `tokens` |
| `declarations[].tokens` | string[] | e.g. `npm.bin`, `npm.private`, `pypi.console_scripts`, `pypi.entry_point:pytest11`, `cargo.lib`, `composer.type:wordpress-plugin`, `maven.packaging:war`, `nuget.output_type:exe`, `mix.escript` |
| `structure` | string[] | Repository-level file-tree tokens: `tree.go_main`, `tree.cargo_main`, `tree.compose`, `tree.k8s`, `tree.tauri`, `tree.browser_extension`, `tree.goreleaser`, … |

Tokens are recorded more generously than they are read. `mix.mod` and
`tree.dockerfile` are both stored and both deliberately unmapped — a library
that starts a supervisor declares the first exactly as an application does, and
a Dockerfile is CI tooling as often as it is the product.

### `data.icon` — the mark that identifies the repository

Which image the catalogue shows for this repository, and where it came from.
Decoration, never scored — it has no metric, no weight and no red flag. The
provenance is the part that carries meaning, and it is why this is a block
rather than a single URL field.

`source_type` distinguishes two very different claims. `avatar` means the mark
identifies the owning **account**: it is shared by every repository that
account publishes, and says nothing about this project. Every other value means
the mark belongs to the project itself. A consumer presenting the icon as
project identity must branch on this field, not on `source_url` being present.

The cascade, the disqualifying rules, and the reason each one exists are
documented in [`scanner/icon.py`](../src/scanner/icon.py) and in the
Repository icons section of `docs/architecture.md`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `collected` | bool | The cascade ran. `false` on reports written before schema 0.31.0 — which is not the same as a cascade that ran and resolved nothing (that is `true` with a null `source_type`) |
| `source_type` | string? | `nuget` \| `homepage` \| `readme` \| `tree` \| `avatar`. Null when nothing validated |
| `source_url` | string? | The exact URL the image was fetched from |
| `media_type` | string? | Sniffed from the bytes, not taken from the server's header |
| `width`, `height` | int? | Intrinsic size, read from the image header or an SVG's root element |
| `bytes` | int? | Size of the original image |
| `content_hash` | string? | SHA-256 of the raw image. The website's cache key, and how one platform's stock icon is spotted appearing under many unrelated owners |
| `candidates_considered` | int | How many URLs were fetched before one validated |
| `rejected[]` | object[] | What was tried and refused: `source_type`, `url`, `reason` (e.g. `not square enough (500x230)`, `stock icon of Read the Docs`). Bounded; present so "why is this project wearing its owner's avatar" has an answer |

The bytes are not in the report. Reports are JSON; the website fetches
`source_url` once and caches it by `content_hash`.

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
| `toolchain_manifests` | string[] | Manifests whose toolchain defines the build/test command itself (`Cargo.toml`, `go.mod`, `mix.exs`, `pom.xml`, `build.gradle`, `*.csproj`) — a weaker bootstrap signal than a task runner, but a real one. Excludes `package.json` and `pyproject.toml`: those ecosystems define no universal test command, and whether one exists lives in file contents this scan does not read |
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
  "classification": { /* what the repository builds — see below */ },
  "categories": [
    {
      "key": "vitality",
      "name": "Vitality",
      "description": "...",
      "weight": 0.21,            // weight in the overall score
      "value": 75,               // category rollup, 1..100 (null if unscored)
      "band": "good",
      "metrics": [ /* Metric objects in this category */ ]
    }
    // community, governance, engineering, security ...
  ]
}
```

### `metrics.classification` — how the software is consumed

Derived from `data`, not observed, which is why it sits in `metrics`: a
rescore recomputes it from stored facts exactly as it recomputes scores. Absent
on reports produced before metrics 2.3.0.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `labels` | string[] | The most specific supported label per branch, strongest first. The vocabulary is a two-level tree (since metrics 2.4.0): top-level `library`, `application`, `host-extension`, `notebook`; subtypes `cli`, `tui`, `desktop`, `mobile`, `web-ui`, `network-service`, `chat-bot`, `mcp-server` under application and `plugin`, `browser-extension`, `editor-extension`, `theme` under host-extension. A bare top-level label appears when the evidence proves the branch but not the subtype (Go's `cmd/` proves an executable, not what kind). Empty when the evidence does not answer the question. Reports written by metrics 2.3.x carry the earlier flat vocabulary (`framework`, `sdk`, `api-client`, `middleware`, `driver`, `desktop-app`, `mobile-app`, `extension`, `ide-tooling`) until rescored |
| `top` | string[] | The top-level labels present — what the three flags derive from. A subtype in `labels` always implies its parent here. Absent on reports written before metrics 2.4.0 |
| `primary` | string? | The best-supported label. **For display and comparison cohorts only** — never the basis of a scoring decision |
| `consumed_by_code` | bool | Other software can depend on this (top-level `library`) |
| `runs_as_process` | bool | Executed rather than imported (top-level `application`) |
| `host_extension` | bool | Installs into a host that supplies its trust model (top-level `host-extension`) |
| `confidence` | string | `high` when a build manifest declared the primary label outright, down to `none` when nothing did |
| `scores` | object | Summed evidence weight per label, positive entries only |
| `evidence[]` | object[] | Every observation that moved a label: `label`, `tier` (`declared`, `distribution`, `structure`, `dependencies`, `tags`, `description`), `weight` (negative where the observation *rules a label out*), `source` |
| `artifacts[]` | object[] | Per-manifest breakdown (`path`, `ecosystem`, `labels`), because a monorepo holding a service beside three libraries is not one artifact |

The three booleans are **independent, not exclusive**. A repository that
publishes a library and deploys a service answers yes twice, and under a future
methodology will owe the obligations of both rather than the laxest of them.

Categories present in a repository report: `vitality`, `community`,
`governance`, `engineering`, `security`, `ai_readiness` (a category with no
scorable metric is omitted). `overall.inputs` normally holds the per-category
values that fed the mean, plus `weighted_overall_raw` and `calibration` — the
raw weighted mean before the fixed calibration curve mapped it onto the
published index scale, and the calibration snapshot identifier (see
`src/scanner/calibration.py`). When evidence triggers the High-Risk
Jurisdiction Policy it additionally records
`weighted_overall_before_jurisdiction`, `high_risk_jurisdiction_multiplier`,
`overall_after_jurisdiction_multiplier`, and `high_risk_jurisdiction_cap`.

When the Abandonment Policy flags the repository it likewise records
`weighted_overall_before_abandonment`, `abandonment_state`,
`abandonment_multiplier`, `overall_after_abandonment_multiplier`, and
`abandonment_cap` (`null` for `at_risk`, which multiplies without a ceiling).
The `abandonment` metric itself sits in the `vitality` category at weight
**0.0**; its `value` *is* the multiplier as a percentage, and its `inputs`
carry the full evidence — `state`, `signals`, `guards`, `declared_reason`,
`unverified_reason`, `days_since_last_human_commit` (with
`…_is_floor` when the sampled window was entirely automation),
`unanswered_open_prs`, `unanswered_open_issues`, and
`days_since_last_merged_pr`. See
[metrics.md](metrics.md#abandonment-policy).

To find a single metric, scan `categories[].metrics[]` for a matching `key`
(the `Metrics.by_key()` / `Metrics.category()` helpers do this in code).

Each metric object:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `key` | string | Stable machine identifier |
| `name` | string | Human-readable name |
| `value` | int | Score, always **1..100**, higher is better |
| `band` | string | Standardized interval: `critical` / `at_risk` / `weak` / `moderate` / `good` / `excellent` / `exceptional` |
| `components` | array | Per-criterion breakdown of the score (see below) |
| `inputs` | object | The raw data values the score was computed from (transparency/audit trail) |
| `note` | string? | Caveats, e.g. components excluded due to missing data |

Each entry in `components`:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `key` | string | Stable slug of the criterion, derived from its English name (`openssf_scorecard_signed_releases`). The handle a localized surface renders a translated label from; the name may be reworded, the key does not change |
| `name` | string | Criterion name (matches docs/metrics.md) |
| `points` | float | Points earned (0 when excluded) |
| `max_points` | float | Weight of the criterion within the metric |
| `status` | string | `met` (full points) / `partial` (some) / `missed` (zero) / `excluded` (no data or not applicable; removed from scoring, weights renormalized) |
| `detail` | string? | The observed value behind the outcome, e.g. `"last push 2 days ago"` |
| `details` | object[] | The same observation as `detail`, machine-identified — the same `{ code, params }` shape as [`notes`](#notes--the-note-machine-identified), concatenated in order. Empty where the text came from outside the scanner (an OpenSSF Scorecard reason, a registry's deprecation note, a URL) and is not ours to restate |

The metric's `value` is normally `round(100 × Σpoints / Σmax_points)` over the
non-excluded components (clamped to 1..100). The documented exception is
`security_posture` when public evidence triggers the high-risk jurisdiction
policy: components produce the base posture, then the policy multiplier and 49
ceiling produce the displayed value. Its `inputs` record the base, multiplier,
multiplied value, and ceiling.
Category and `overall` rollups have no components; their `inputs` carry the
child values and any documented policy adjustment.

`popularity` carries the growth-authenticity assessment in its `inputs`, since
the Inorganic Growth Policy is a factor over its stars and forks components
rather than a metric of its own (see
[metrics.md](metrics.md#inorganic-growth-policy)):

| Input | Description |
| ----- | ----------- |
| `growth_state` | `organic` / `unverified` / `anomalous` / `highly_anomalous` |
| `growth_factor_pct` | The factor applied to stars and forks, as a percentage (100 when nothing is discounted) |
| `growth_unverified_reason` | Present only when `unverified`: `no_history`, `below_threshold`, or `window_too_short` |
| `growth_signals` | Present only when flagged: `acquisition_burst` plus the corroborating signal keys |
| `growth_peak_window` | `"YYYY-MM-DD"` or `"YYYY-MM-DD → YYYY-MM-DD"` — the largest confirmed burst |
| `growth_peak_stars`, `growth_peak_days`, `growth_peak_multiple` | The burst's size, length, and multiple of the repository's own daily baseline |
| `growth_top_days_share` | Share of collected stars that arrived on the five busiest days (0–1) |
| `growth_baseline_per_day` | The median active-day star rate the multiple is measured against |
| `growth_history_complete` | Whether the collected star history reached the beginning of the repository |

A metric is `null` when none of its inputs could be collected — **missing
data is never silently scored**.

### `notes` — the note, machine-identified

`note` is generated English prose, and generated text cannot be translated by
whatever renders it. Every statement `note` makes is therefore also reported in
`notes`, as a code and the values it is about, so a localized surface can state
the same thing in the reader's language:

```jsonc
"note": "Excluded from scoring (no data or not applicable): Dependency lockfiles. Remaining weights renormalized.",
"notes": [
  { "code": "excluded_no_data", "params": { "components": ["dependency_lockfiles"] } },
  { "code": "weights_renormalized", "params": {} }
]
```

`note` stays authoritative for consumers that read prose, and its wording is
unchanged by the addition. Codes currently emitted: `excluded_no_data`,
`disabled_in_config`, `weights_renormalized`, `categories_no_data`,
`categories_disabled`, `category_weights_renormalized`,
`advisories_scope_published`, `advisories_scope_repository`,
`advisories_repo_graph_caveat`, `advisories_unassessed`,
`advisories_reachability`, `jurisdiction_evidence_limits`,
`jurisdiction_below_threshold`, `jurisdiction_posture_adjustment`,
`jurisdiction_overall_adjustment`, `growth_policy_discount`. Component and category references are keys, not
display names, for the same reason.

## Organization report

Produced when the scan target is an organization (`inspect-scan orgname`).

```jsonc
{
  "report_type": "organization",
  "schema_version": "0.30.0",
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
