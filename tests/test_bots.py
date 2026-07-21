"""Commit authorship classification: bot accounts vs coding agents."""

from __future__ import annotations

import pytest

from scanner.bots import classify_commit

NOREPLY = "{id}+{login}@users.noreply.github.com"


def classify(login=None, name=None, email=None, message="Fix the thing"):
    return classify_commit(
        author_login=login, author_name=name, author_email=email, message=message
    )


# --- bot accounts ----------------------------------------------------------

@pytest.mark.parametrize(
    "login",
    ["dependabot[bot]", "renovate[bot]", "kubernetes-prow[bot]", "github-actions[bot]"],
)
def test_bot_login_suffix_is_the_marker(login):
    # Verified against live history: GraphQL's author.user.__typename answers
    # "User" for all of these, so the suffix is all there is to go on.
    assert classify(login=login).is_bot is True


def test_bot_detected_from_the_git_name_alone():
    # Commits pushed by an app the scan could not resolve to an account still
    # carry the app's name in the git author field.
    assert classify(login=None, name="renovate[bot]").is_bot is True


def test_bot_detected_from_the_noreply_address():
    email = NOREPLY.format(id=49699333, login="dependabot[bot]")
    assert classify(login=None, name="dependabot", email=email).is_bot is True


def test_human_is_not_a_bot():
    result = classify(login="davidism", name="David Lord", email="david@example.com")
    assert result == (False, False)


def test_human_whose_name_merely_contains_bot():
    assert classify(login="robotnik", name="Bot Robertson").is_bot is False


# --- coding agents ---------------------------------------------------------

@pytest.mark.parametrize(
    "login",
    [
        "copilot-swe-agent[bot]",
        "claude[bot]",
        "devin-ai-integration[bot]",
        "chatgpt-codex-connector[bot]",
        "google-labs-jules[bot]",
        "gemini-code-assist[bot]",
    ],
)
def test_agent_committing_under_its_own_account_is_both(login):
    # These are GitHub Apps, so they are bots as well — the flags are
    # independent, not mutually exclusive.
    assert classify(login=login) == (True, True)


def test_agent_credited_as_co_author_of_a_human_commit():
    # The common shape: a person committed, an agent wrote it.
    message = (
        "Add per-day fork history\n\n"
        "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
    )
    result = classify(login="Nayjest", name="Vitalii", message=message)
    assert result.is_coding_agent is True
    assert result.is_bot is False


@pytest.mark.parametrize(
    "trailer",
    [
        "Co-authored-by: Cursor Agent <cursoragent@cursor.com>",
        "Co-authored-by: Cursor Agent <noreply@cursor.com>",
        "Co-authored-by: aider <aider@aider.chat>",
        "Co-authored-by: openhands <openhands@all-hands.dev>",
    ],
)
def test_agent_trailer_addresses(trailer):
    assert classify(login="human", message=f"Do a thing\n\n{trailer}").is_coding_agent


def test_agent_co_author_via_its_noreply_address():
    # Copilot Autofix credits itself through GitHub's forwarding address.
    email = NOREPLY.format(id=223894421, login="github-code-quality[bot]")
    message = f"Improve error message (#19671)\n\nCo-authored-by: Copilot Autofix <{email}>"
    assert classify(login="fisker", message=message).is_coding_agent is True


def test_agent_named_in_the_trailer_but_not_in_its_address():
    # Real axios trailer: the forwarding address says "Copilot", the display
    # name carries the actual login.
    trailer = "Co-authored-by: copilot-swe-agent[bot] <198982749+Copilot@users.noreply.github.com>"
    assert classify(login="human", message=f"Bump js-yaml\n\n{trailer}").is_coding_agent


def test_generated_with_phrase_without_a_trailer():
    message = "Refactor the parser\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
    assert classify(login="human", message=message).is_coding_agent is True


# --- the false positives that matter --------------------------------------

def test_ordinary_bot_is_not_a_coding_agent():
    # A dependency bumper writes no code of its own.
    message = "chore(deps): bump js-yaml\n\nSigned-off-by: dependabot[bot] <support@github.com>"
    assert classify(login="dependabot[bot]", message=message) == (True, False)


def test_human_co_author_is_not_an_agent():
    message = "Fix the thing\n\nCo-authored-by: Jason Saayman <jasonsaayman@gmail.com>"
    assert classify(login="shaanmajid", message=message).is_coding_agent is False


def test_repository_that_merely_talks_about_agents():
    # The whole reason phrases are product-specific rather than vendor names:
    # this project's own commits discuss Claude constantly.
    message = "Add Claude Code support to the agent-readiness signals\n\nDocuments cursor and devin too."
    assert classify(login="Nayjest", message=message).is_coding_agent is False


def test_missing_everything_is_not_a_bot():
    assert classify(login=None, name=None, email=None, message=None) == (False, False)
