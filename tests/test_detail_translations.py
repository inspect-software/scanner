"""Every detail code the scanner emits has a translation in every locale.

The scanner stores each component observation twice: as generated English
prose (``MetricComponent.detail``) and as a code plus parameters
(``MetricComponent.details``), so the web application can restate it in the
reader's language. The lookup is all-or-nothing per phrase — one missing
``detail.*`` key drops the whole phrase back to English — and it is silent.

That is not hypothetical. Eight ``abandonment_*`` codes shipped without
translations and rendered in English on every localized page until someone
counted the keys by hand. Nothing failed, because nothing was checking.

This test lives in the scanner suite because the scanner is what CI runs, and
because the scanner is the side that adds codes. It skips rather than fails
when the website is not checked out beside it, so the package stays usable on
its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scanner.metrics import DETAIL_TEMPLATES

LOCALES = ("en", "de", "es", "uk", "zh")
MESSAGES = Path(__file__).resolve().parents[2] / "website" / "frontend" / "src" / "i18n" / "messages"
_DETAIL_KEY = re.compile(r"'detail\.([a-z0-9_]+)'")


def _translated_codes(locale: str) -> set[str]:
    path = MESSAGES / f"{locale}.js"
    if not path.exists():
        pytest.skip(f"website messages not present at {MESSAGES}")
    return set(_DETAIL_KEY.findall(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("locale", LOCALES)
def test_every_detail_code_is_translated(locale: str):
    missing = sorted(set(DETAIL_TEMPLATES) - _translated_codes(locale))
    assert not missing, (
        f"{locale}.js is missing detail.* keys for {missing}. "
        "A metric detail without a translation renders in English for every "
        "reader of that language."
    )


@pytest.mark.parametrize("locale", LOCALES)
def test_no_translations_for_codes_the_scanner_no_longer_emits(locale: str):
    """A removed code leaves dead weight behind, and the next reader cannot
    tell it from a key that is merely unused yet."""
    stale = sorted(_translated_codes(locale) - set(DETAIL_TEMPLATES))
    assert not stale, f"{locale}.js translates detail codes the scanner does not emit: {stale}"
