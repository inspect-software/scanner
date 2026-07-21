"""Classify who — or what — authored a commit.

Two independent questions, deliberately not collapsed into one flag:

``is_bot``
    The *account* that authored the commit is an automation account, not a
    person: Dependabot, Renovate, a CI bot pushing generated files.

``is_coding_agent``
    An LLM coding agent *wrote the change*. This says nothing about who
    committed it — the common case is a human author whose message carries a
    ``Co-authored-by`` trailer naming the agent, so the commit is authored by a
    person and produced by a machine at the same time. A commit can be both
    (an agent's own GitHub App pushing directly), either, or neither.

Detection keys off identifiers GitHub controls, not free text:

- The ``[bot]`` login suffix. GitHub reserves it for GitHub Apps, and REST
  ``/users/{login}`` confirms ``"type": "Bot"`` for every one of them. The
  GraphQL commit history cannot be used for this — ``author.user.__typename``
  answers ``User`` even for ``renovate[bot]`` (verified against vuejs/core,
  prettier, axios and kubernetes).
- Verified account logins for the agents that commit under their own identity.
- Trailer email addresses for the agents that run on a developer's machine and
  sign the work as a co-author.

Both lists are inevitably incomplete — new agents appear constantly. A missed
agent reads as an ordinary human commit, which is the safe direction to fail:
this data feeds descriptive statistics, and inventing an agent that was not
there would be worse than missing one that was.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

BOT_LOGIN_SUFFIX = "[bot]"

# Coding-agent GitHub Apps, each verified to exist and report "type": "Bot"
# via /users/{login}; the database id is recorded because logins can be renamed
# while ids cannot.
CODING_AGENT_LOGINS = {
    "copilot-swe-agent[bot]",        # 198982749 — GitHub Copilot coding agent
    "github-code-quality[bot]",      # 223894421 — Copilot Autofix
    "claude[bot]",                   # 209825114 — Claude GitHub App
    "devin-ai-integration[bot]",     # 158243242 — Devin
    "google-labs-jules[bot]",        # 161369871 — Jules
    "chatgpt-codex-connector[bot]",  # 199175422 — OpenAI Codex
    "gemini-code-assist[bot]",       # 176961590 — Gemini Code Assist
    "cubic-dev-ai[bot]",             # 191113872 — cubic
    "coderabbitai[bot]",             # 136622811 — CodeRabbit
    "greptile-apps[bot]",            # 165735046 — Greptile
    "ellipsis-dev[bot]",             # 65095814  — Ellipsis
    "sourcery-ai[bot]",              # 58596630  — Sourcery
}

# Addresses agents sign co-author trailers with when they run locally, under
# the developer's own git identity. Matched on the full address, not the domain
# alone: a vendor's corporate domain also carries its employees' human mail.
CODING_AGENT_EMAILS = {
    "noreply@anthropic.com",      # Claude Code
    "cursoragent@cursor.com",     # Cursor Agent
    "noreply@cursor.com",         # Cursor Agent (newer form)
    "aider@aider.chat",           # Aider
    "openhands@all-hands.dev",    # OpenHands
}

# Phrases agents append to the message itself rather than to a trailer. Kept
# narrow and product-specific on purpose — matching a bare vendor name would
# flag every commit in a repository that merely talks about the tool.
CODING_AGENT_PHRASES = (
    "generated with claude code",
    "generated with [claude code]",
    "co-authored-by: copilot",
    "co-authored-by: cursor agent",
    "co-authored-by: devin",
)

_TRAILER_RE = re.compile(r"^co-authored-by:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_EMAIL_RE = re.compile(r"<([^>]+)>")
# GitHub's forwarding address embeds the account: 49699333+dependabot[bot]@…
_NOREPLY_RE = re.compile(r"^\d+\+(?P<login>.+)@users\.noreply\.github\.com$", re.IGNORECASE)


class CommitAuthorship(NamedTuple):
    is_bot: bool
    is_coding_agent: bool


def classify_commit(
    author_login: Optional[str],
    author_name: Optional[str],
    author_email: Optional[str],
    message: Optional[str],
) -> CommitAuthorship:
    """Classify one commit's authorship.

    ``message`` should be the *whole* message (headline and body) exactly as
    written — trailers are its last lines, and classifying a shortened body
    would silently lose them.
    """
    return CommitAuthorship(
        is_bot=_is_bot(author_login, author_name, author_email),
        is_coding_agent=_is_coding_agent(author_login, author_email, message),
    )


def _account_of(email: Optional[str]) -> Optional[str]:
    """Login encoded in a GitHub noreply forwarding address, if it is one."""
    match = _NOREPLY_RE.match((email or "").strip())
    return match.group("login").lower() if match else None


def _is_bot(login: Optional[str], name: Optional[str], email: Optional[str]) -> bool:
    candidates = [(login or "").lower(), (name or "").lower(), _account_of(email) or ""]
    return any(value.endswith(BOT_LOGIN_SUFFIX) for value in candidates)


def _is_coding_agent(
    login: Optional[str], email: Optional[str], message: Optional[str]
) -> bool:
    # The agent committed under its own account.
    for account in ((login or "").lower(), _account_of(email) or ""):
        if account in CODING_AGENT_LOGINS:
            return True

    text = message or ""
    lowered = text.lower()
    if any(phrase in lowered for phrase in CODING_AGENT_PHRASES):
        return True

    # A human commit crediting an agent as co-author. Both halves of the
    # trailer are checked: the forwarding address does not always carry the
    # full login (Copilot signs as "198982749+Copilot@users.noreply.github.com"
    # while naming itself copilot-swe-agent[bot] in the display name).
    for trailer in _TRAILER_RE.findall(text):
        display_name = _EMAIL_RE.split(trailer, maxsplit=1)[0].strip().lower()
        if display_name in CODING_AGENT_LOGINS:
            return True
        for address in _EMAIL_RE.findall(trailer):
            address = address.strip().lower()
            if address in CODING_AGENT_EMAILS:
                return True
            if (_account_of(address) or "") in CODING_AGENT_LOGINS:
                return True
    return False
