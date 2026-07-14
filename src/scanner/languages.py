"""Significant-language derivation from GitHub's language byte map.

GitHub's ``/languages`` endpoint reports *every* language linguist detects,
down to a 40-byte shell script. For "which languages is this repository
actually written in?" that raw map over-reports: nearly every project carries
traces of Shell, Dockerfile, Makefile, HTML or CSS. The rule here is the one
definition used everywhere a language list is shown — the scanner HTML
report, the website catalogue cards, and the package profile page:

- a language is **significant** when it accounts for at least
  ``MIN_SHARE`` (10%) of the repository's total language bytes;
- languages are ordered by bytes, largest first, so the first entry matches
  GitHub's ``primary_language``;
- the list is capped at ``MAX_LANGUAGES`` entries (displays typically show
  the first three);
- when the byte map is empty (empty repo, or the languages API failed) the
  list falls back to ``primary_language`` alone, so a report always carries
  the best available answer.

No allowlist of "real" programming languages is applied: for a docs or
design-system repository, HTML *is* the language. The share threshold alone
separates substance from residue.
"""

from __future__ import annotations

from typing import Optional

# A language below this share of total bytes is residue (scripts, generated
# docs, CI glue), not something the repository "is written in".
MIN_SHARE = 0.10

# Data-layer cap; UI surfaces typically show the first three.
MAX_LANGUAGES = 5


def significant_languages(
    languages: dict[str, int], primary_language: Optional[str] = None
) -> list[str]:
    """Languages holding >= MIN_SHARE of total bytes, largest first.

    ``primary_language`` is only a fallback for an empty/unavailable byte map;
    when the map is present, GitHub's primary language is by definition the
    largest entry and leads the list naturally.
    """
    total = sum(b for b in languages.values() if b > 0)
    if total <= 0:
        return [primary_language] if primary_language else []
    ranked = sorted(
        ((name, size) for name, size in languages.items() if size > 0),
        key=lambda item: (-item[1], item[0]),
    )
    result = [name for name, size in ranked if size / total >= MIN_SHARE]
    if not result:  # pathological maps (thousands of tiny languages)
        result = [ranked[0][0]]
    return result[:MAX_LANGUAGES]
