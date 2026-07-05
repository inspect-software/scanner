# inspect-scanner

CLI scanner that audits a **public GitHub repository** and produces a
structured **JSON report** with health, maintainability, quality, security,
and dependency signals. Part of the inspect-software auditing/certification
platform.

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
```

Unauthenticated GitHub API access is limited to 60 requests/hour (a scan uses
~12). Set a token for the 5000/hour limit:

```bash
export GITHUB_TOKEN=ghp_...        # or pass --token
```

## What's in the report

The report schema is defined as Pydantic models in
[`src/scanner/models.py`](src/scanner/models.py) and is **versioned**
(`schema_version`). Sections:

| Section           | Contents                                                                 |
| ----------------- | ------------------------------------------------------------------------ |
| `source`          | Repo URL, owner, name                                                     |
| `repo`            | Description, dates, license (SPDX), languages, topics, archived/fork flags |
| `popularity`      | Stars, forks, watchers                                                    |
| `activity`        | Commits/active weeks last year, days since last push, release cadence     |
| `maintainability` | Top contributors, bus factor, issue/PR open/close/merge counts            |
| `community`       | GitHub community profile: README, license, contributing, templates        |
| `quality`         | CI workflows, tests, docs dir, linter configs (file-tree heuristics)      |
| `security`        | SECURITY.md, Dependabot config, CodeQL workflow, lockfiles                |
| `dependencies`    | Dependency manifests found and inferred package ecosystems                |
| `warnings`        | Non-fatal collection issues (rate limits, stats still computing)          |

Notes on semantics:

- **bus_factor** — smallest number of contributors whose commits cover ≥50% of
  sampled commits (top-100 contributors sample).
- File-based signals are heuristics from the git tree of the default branch;
  they indicate *presence*, not quality.
- These are **signals, not warranties**. Scoring/certification happens
  downstream and is versioned separately.

## Development

```bash
uv sync --extra dev
uv run pytest
```

## Exit codes

- `0` — report produced (warnings, if any, go to stderr)
- `1` — GitHub API failure (rate limit, network, repo not found)
- `2` — invalid input (unparseable repo URL)
