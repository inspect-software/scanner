# Security policy

## Reporting a vulnerability

Report privately to **mail@inspect.software** with `security` in the subject
line, or open a [private security advisory][advisory] on this repository. Please
do not open a public issue for a suspected vulnerability.

Include enough to reproduce it safely: the affected component, the steps, the
impact, and a proof of concept if you have one. Do not include tokens, personal
data, or anything belonging to a third party.

You should get an acknowledgement within three working days and an assessment
within ten. If a fix is warranted we will agree a disclosure date with you, and
credit you in the release notes unless you would rather we did not.

[advisory]: https://github.com/inspect-software/scanner/security/advisories/new

## What this scanner does and does not do

Worth stating plainly, because it bounds the threat model and it is the first
thing a reviewer asks:

- It **never clones or executes** the code it audits. It reads the GitHub API,
  fetches raw manifest and metadata files, and queries public package
  registries. Nothing from a scanned repository is run.
- It **parses untrusted input** — READMEs, `package.json`, `pom.xml`, SBOMs,
  HTML `<head>` sections, favicons — all fetched from repositories and
  registries under someone else's control. Parser hardening is in scope: a
  malformed manifest that crashes a scan is a bug, and one that causes the
  process to read a local file, make an unintended request, or consume
  unbounded memory is a vulnerability. XML goes through
  `src/scanner/xmlsafe.py`, which refuses entity declarations; that module
  records what the stdlib parser does and does not refuse on its own.
- It **shells out to OpenSSF Scorecard** when that binary is on PATH
  (`src/scanner/scorecard.py`). Scorecard runs git of its own against the
  public repository. Issues in Scorecard itself belong to
  [ossf/scorecard](https://github.com/ossf/scorecard); issues in how we invoke
  it belong here.
- It **fetches URLs a repository declares** — the homepage, documentation site,
  icon candidates, and the Go vanity path in a `go.mod`. `src/scanner/icon.py`
  refuses private and link-local addresses (`is_public_url`), vets **every hop
  of a redirect chain before making it** (`public_stream`), and caps every
  response. A way past any of those is a server-side request forgery report,
  and in scope. Known limit, stated because it is not fixed: the host name is
  resolved once for the check and again by the HTTP client, so DNS rebinding
  between the two is not stopped.
- It **holds a GitHub token** from the environment or a `.env` file. The token
  needs no more than public read access. A path that writes a token into a
  report, a log line, or an error message is in scope — reports are published.

## Supported versions

The latest release on `main`. This is a young project; there are no maintained
release branches, and a fix ships as a new version rather than as a backport.
