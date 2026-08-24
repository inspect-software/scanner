# Contributing

Thank you for wanting to improve the engine behind the public record.

## First, something unusual about this repository

This repository is **published from a private workspace**, not developed in.
`main` here is produced by replaying every commit that touched the scanner in
that workspace — each one carries a `Workspace-Commit:` trailer naming where it
came from.

Two consequences for you:

- **Pull requests are welcome and are read.** What happens on merge is that a
  maintainer applies the change upstream and it returns here on the next
  publish, so your commit arrives with a different hash. Your authorship is
  preserved; the hash is not. We will say so on the PR rather than leave you
  wondering why it closed without a merge commit.
- **Do not force-push or rewrite `main`.** The trailer on its tip is the only
  thing that tells the publisher where to resume.

If that model rules out the contribution you had in mind, open an issue first
and say so — it is a deliberate choice, but not a permanent one.

## Setting up

```bash
uv sync --extra dev
uv run pytest
```

The suite runs in about ten seconds and touches no network: every HTTP
interaction in the tests is stubbed. If a change you make needs a live request
to be tested, that is a sign the seam is in the wrong place.

Three test modules — `test_methodology_version_sync`, `test_detail_translations`
and `test_content_translations` — compare the scanner against the website that
consumes it, and **skip here** because the website is not in this repository.
They are not dead: they run upstream, and they are the gate on anything that
crosses that boundary. Green here does not mean green there.

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
  ingestion and eligibility policy that has to accompany it live upstream.

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
