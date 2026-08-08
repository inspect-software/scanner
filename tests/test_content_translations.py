"""Translated content pages stay in step with their English source.

``website/frontend/src/content.js`` already computes a ``source-hash`` — djb2
over the English body — that every translated file carries in its frontmatter,
and the SSR server warns at startup when the two disagree. That guard proved
too weak to rely on: the warning prints the expected value, so the cheapest way
to silence it is to paste the new hash into the frontmatter without touching
the prose. Commit db7f95d did exactly that to all four locales of
``ai-agent-context`` after c01ca2d had added a third scoring component in
English, and the four translations went on publishing a two-component table
with the old weights for two weeks — green hash, wrong numbers.

So the hash is checked here, and two things it cannot see are checked beside
it:

* **Structure.** Headings, table rows and list items are counted. A section or
  a table row added in English and skipped in a translation shows up as a count
  mismatch, whatever the prose says.
* **Table numbers.** Every number inside a table row must appear the same
  number of times in the translation. Prose is deliberately excluded — English
  writes "high 70s" where Spanish writes "los setenta altos", and no useful
  test can reconcile those. Weights and thresholds live in tables, which is
  also where every drift found so far actually was.

This lives in the scanner suite for the same reasons as
``test_methodology_version_sync``: CI runs the scanner, and the scanner is the
side that changes what the tables have to say. It skips rather than fails when
the website is not checked out beside it.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

CONTENT = Path(__file__).resolve().parents[2] / "website" / "frontend" / "content"
KINDS = ("pages", "wiki")
# Mirrors LOCALES in frontend/src/i18n/locales.js, minus the English default.
LOCALES = ("es", "uk", "de", "zh")

_FRONTMATTER = re.compile(r"^---\n([\s\S]*?)\n---\n?")
_META_LINE = re.compile(r"^([a-zA-Z_-]+):\s*(.*)$")
_HEADING = re.compile(r"^#{2,4} ")
_TABLE_DIVIDER = re.compile(r"^\|[\s\-:|]+\|?$")
_LIST_ITEM = re.compile(r"^\s*([-*]|\d+\.) ")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A run of digits, tolerating '.', ',' and NBSP as group or decimal marks:
# 46,889 / 46 889 / 13.5 / 13,5 all have to reduce to the same value.
_NUMBER = re.compile(r"\d[\d  ,.]*\d|\d")
_THOUSANDS = re.compile(r"^\d{1,3}([.,]\d{3})+$")


def _parse(raw: str) -> tuple[dict[str, str], str]:
    """Frontmatter and body, matching parseFrontmatter in content.js."""
    raw = raw.replace("\r\n", "\n")
    meta: dict[str, str] = {}
    match = _FRONTMATTER.match(raw)
    if not match:
        return meta, raw
    for line in match.group(1).split("\n"):
        kv = _META_LINE.match(line)
        if kv:
            meta[kv.group(1)] = kv.group(2).strip().strip("\"'").strip()
    return meta, raw[match.end() :]


def _content_hash(text: str) -> str:
    """djb2 over UTF-16 code units — contentHash() in content.js, in Python.

    JavaScript's charCodeAt yields UTF-16 code units, so anything outside the
    BMP has to be split into a surrogate pair or the hashes never agree on the
    Chinese pages.
    """
    h = 5381
    for ch in text:
        cp = ord(ch)
        units = (
            (0xD800 + ((cp - 0x10000) >> 10), 0xDC00 + ((cp - 0x10000) & 0x3FF))
            if cp > 0xFFFF
            else (cp,)
        )
        for unit in units:
            h = ((h * 33) ^ unit) & 0xFFFFFFFF
    return format(h, "x")


def _shape(body: str) -> dict[str, int]:
    lines = body.split("\n")
    return {
        "headings": sum(1 for line in lines if _HEADING.match(line)),
        "table rows": sum(
            1 for line in lines if line.startswith("|") and not _TABLE_DIVIDER.match(line)
        ),
        "list items": sum(1 for line in lines if _LIST_ITEM.match(line)),
    }


def _table_numbers(body: str) -> Counter[float]:
    """Every number appearing inside a table row, normalized to a value."""
    rows = [
        line
        for line in body.split("\n")
        if line.startswith("|") and not _TABLE_DIVIDER.match(line)
    ]
    text = _ISO_DATE.sub("", _LINK_TARGET.sub("]()", "\n".join(rows)))
    found: Counter[float] = Counter()
    for token in _NUMBER.findall(text):
        token = token.replace(" ", "").replace(" ", "")
        if "," in token and "." in token:
            token = token.replace(",", "")
        elif _THOUSANDS.match(token):
            token = re.sub(r"[.,]", "", token)
        else:
            token = token.replace(",", ".")
        token = token.rstrip(".")
        try:
            found[float(token)] += 1
        except ValueError:
            continue
    return found


def _cases() -> list[tuple[str, str, str]]:
    if not CONTENT.is_dir():
        return []
    return [
        (kind, source.name, locale)
        for kind in KINDS
        for source in sorted((CONTENT / kind).glob("*.md"))
        for locale in LOCALES
    ]


@pytest.fixture(scope="module")
def content_root() -> Path:
    if not CONTENT.is_dir():
        pytest.skip("website not checked out beside the scanner")
    return CONTENT


@pytest.mark.parametrize(
    ("kind", "name", "locale"),
    _cases(),
    ids=lambda part: str(part).removesuffix(".md"),
)
def test_translation_tracks_its_english_source(
    content_root: Path, kind: str, name: str, locale: str
) -> None:
    english = content_root / kind / name
    translated = english.parent / locale / name
    slug = f"{locale}/{kind}/{english.stem}"

    assert translated.exists(), (
        f"{slug} is missing. The site falls back to English for it, so the page "
        f"silently ships untranslated."
    )

    meta, body = _parse(translated.read_text(encoding="utf-8"))
    _, source_body = _parse(english.read_text(encoding="utf-8"))
    expected_hash = _content_hash(source_body)

    assert meta.get("source-hash") == expected_hash, (
        f"{slug} carries source-hash {meta.get('source-hash')!r} but the English "
        f"body now hashes to {expected_hash!r}. Translate the change, then set "
        f"the new value — updating the hash alone is what let the AI Readiness "
        f"weights drift for two weeks."
    )

    english_shape, translated_shape = _shape(source_body), _shape(body)
    for part, expected in english_shape.items():
        assert translated_shape[part] == expected, (
            f"{slug} has {translated_shape[part]} {part}, English has {expected}. "
            f"A section, table row or bullet was added or dropped on one side."
        )

    expected_numbers = _table_numbers(source_body)
    actual_numbers = _table_numbers(body)
    missing = expected_numbers - actual_numbers
    extra = actual_numbers - expected_numbers
    assert not missing and not extra, (
        f"{slug} disagrees with English on the numbers in its tables: "
        f"only in English {sorted(missing.elements())}, "
        f"only in {locale} {sorted(extra.elements())}. These are weights and "
        f"thresholds — the translation is publishing a different methodology."
    )
