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

The report has two strictly separated layers (schema defined as Pydantic
models in [`src/scanner/models.py`](src/scanner/models.py)):

- **`data`** — raw facts observed from the GitHub API: repo metadata,
  popularity, commit/release activity, contributors and bus factor, issue/PR
  counts, community profile, and file-tree signals (CI, tests, linting,
  security policy, lockfiles, dependency manifests). No judgement, no scoring.
- **`metrics`** — standardized scores, each an integer **1..100** mapped to a
  band (`critical` / `at_risk` / `moderate` / `good` / `excellent`):
  `activity`, `maintainer_resilience`, `responsiveness`, `community_health`,
  `engineering_practices`, `security_posture`, and a weighted `overall`.
  Every metric echoes the raw `inputs` it was computed from.

Both layers are independently versioned (`schema_version` for the structure,
`metrics_version` for the scoring methodology). Full documentation:

- [docs/report-schema.md](docs/report-schema.md) — field-by-field schema
- [docs/metrics.md](docs/metrics.md) — bands, formulas, weights, worked example

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
