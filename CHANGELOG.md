# Changelog

All notable changes to the scanner are recorded here, newest first. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow the contract below, which is written for the consumer that pins this
package — the inspect.software backend — as much as for anyone else.

**Version contract**

- **patch** — no report field and no score changes for any repository.
- **minor** — a new metric, ecosystem, or report field; any `METRICS_VERSION`
  or `SCHEMA_VERSION` bump. The consumer must expect scores or report shapes
  to move.
- **major** — a breaking change to the Python API the backend imports
  (`compute_metrics`, `Report`, the boundary names guarded by
  `tests/test_backend_boundary.py`, and friends).

**Downstream impact** is a required line on every entry: it states what the
consumer has to do when taking the release — nothing, rebuild only, bump the
website's `METHODOLOGY_VERSION`, or a full rescore. A `METRICS_VERSION` bump
always means the deployed record rescored against the new methodology, which
is an operation, not a side effect — say so here so the pin bump that carries
it is deliberate.

## [0.12.0] — 2026-08-27

### Changed
- Cross-repository tests (`test_methodology_version_sync`,
  `test_detail_translations`, `test_content_translations`) moved to the
  workspace, where the website they compare against actually is. They had
  skipped here since the split; nothing this package tests on its own changed.

### Added
- `tests/test_backend_boundary.py`: the names the inspect.software backend
  reaches into this package for (`github.rate_limit_observer`,
  `github._token_cooldowns`, `github.token_fingerprint`,
  `ecosystems.FETCHERS`) are now asserted as public API. Renaming or
  reshaping any of them is a major version bump.
- This changelog, and the tag-driven release workflow.

**Downstream impact:** nothing — no scoring, schema, or API change.

## [0.11.0] — 2026-08-24

The state of the package at the point it gained its own public repository
(AGPL-3.0-or-later, split from the inspect.software workspace with history).
At this release: `METRICS_VERSION` 2.10.0, `SCHEMA_VERSION` 0.34.0. Earlier
history is in the git log, which runs back through the workspace to the
scanner's original standalone repository.
