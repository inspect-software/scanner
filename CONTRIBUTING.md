# Contributing

Thank you for wanting to improve the engine behind the public record.

## How changes land

This repository is where the scanner is developed: pull requests merge here,
normally. (Until 2026-08 it was published from a private workspace by
replaying commits — the `Workspace-Commit:` trailers in the older history are
that era's bookkeeping, not something a new change carries.)

What consumes it — the inspect.software application — pins **tagged
releases** of this package. So a merged change reaches the public record when
a maintainer cuts the next release: version bumped in `pyproject.toml` and
`__version__`, a `CHANGELOG.md` entry with its **Downstream impact** line, and
a `vX.Y.Z` tag, which the Release workflow turns into a GitHub release.
Released tags are never moved or deleted.

## Setting up

```bash
uv sync --extra dev
uv run pytest
```

The suite runs in about ten seconds and touches no network: every HTTP
interaction in the tests is stubbed. If a change you make needs a live request
to be tested, that is a sign the seam is in the wrong place.

The website that consumes this package keeps its own tests comparing the two —
methodology version sync, detail-code translations, content translations. They
run in that repository, not here, and they are the gate on anything that
crosses the boundary: green here does not mean green there. On this side,
`tests/test_backend_boundary.py` pins the names the consumer imports; changing
one is a breaking release, not a refactor.

## What a good change looks like

- **Comments say why, not what.** The codebase is dense with the reasoning
  behind decisions — which alternative was tried, which data made it wrong.
  Match that. A comment restating the line below it will be asked about.
- **Tests come with behaviour.** Every scoring rule, parser and heuristic here
  has tests naming the case it exists for. A bug fix should carry the input that
  used to break it.
- **Missing data is never a zero.** When a signal cannot be observed, exclude it
  and renormalize the remaining weights. Scoring an unknown as zero
  misrepresents the project, and the whole methodology rests on not doing it.
- **Signals, not warranties.** Nothing here is a code audit or a security
  guarantee, and no message, note or metric name should imply that it is.

## Changes that need a maintainer

Some things cannot be completed from this repository alone. Propose them, but
expect a maintainer to finish them:

- **`METRICS_VERSION` (`src/scanner/metrics.py`).** Bumping it rescores the
  entire public record and requires the website's own constant to move in the
  same release. Change scoring behaviour in your PR if you have a case for it;
  leave the version to us.
- **`SCHEMA_VERSION` (`src/scanner/models.py`)** and the calibration table in
  `src/scanner/calibration.py`, for the same reason — stored reports are
  replayed through both.
- **New ecosystems.** The registry side is straightforward; the catalogue
  ingestion and eligibility policy that has to accompany it live in the
  application that consumes this package.

## Contributor Licence Agreement

The project is AGPL-3.0-or-later, and commercial licences are sold to fund it.
Selling one requires the right to license every line in the release, which a
contribution does not grant by default. So a first pull request needs
[CLA.md](CLA.md) signed — once, then never again.

It is a licence grant, not a copyright assignment: **you keep the copyright in
your work.** Read it before signing; if something in it stops you, say so on the
issue and we will talk rather than lose the contribution.

## Reporting bugs and vulnerabilities

Ordinary bugs: open an issue with the repository you scanned, the command, and
what you expected. A scan is reproducible from its URL, which makes almost every
report actionable.

Security issues: **not** in a public issue — see [SECURITY.md](SECURITY.md).
