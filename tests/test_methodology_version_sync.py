"""The website's displayed methodology version matches ``METRICS_VERSION``.

``website/frontend/src/store.js`` carries a hand-maintained copy of the metrics
version. It feeds the nav badge, the home and Insights stat tooltips, the wiki
index kicker, the Compare kicker and the catalogue scan notes — every surface
except the report page, which prefers the version recorded in the report itself.

Nothing enforced the copy, and it drifted: the constant sat at 2.1.0 while the
scanner, the methodology page and the version history all said 2.3.1, so the
site advertised one version in its chrome and another in its own documentation.
Three consecutive bumps went unnoticed because a missed edit here breaks
nothing that runs.

This test lives in the scanner suite for the same reasons as
``test_detail_translations``: the scanner is what CI runs, and the scanner is
the side that bumps the version. It skips rather than fails when the website is
not checked out beside it, so the package stays usable on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scanner.metrics import METRICS_VERSION

STORE = Path(__file__).resolve().parents[2] / "website" / "frontend" / "src" / "store.js"
_CONSTANT = re.compile(r"""^export const METHODOLOGY_VERSION = ['"]([^'"]+)['"]""", re.M)


def test_website_methodology_version_matches_scanner() -> None:
    if not STORE.exists():
        pytest.skip("website not checked out beside the scanner")

    match = _CONSTANT.search(STORE.read_text(encoding="utf-8"))
    assert match, f"METHODOLOGY_VERSION not found in {STORE}"

    assert match.group(1) == METRICS_VERSION, (
        f"{STORE.name} declares METHODOLOGY_VERSION {match.group(1)!r} but the "
        f"scanner is at {METRICS_VERSION!r}. Bump the constant so the site's "
        f"nav badge, kickers and scan notes stop contradicting its reports."
    )
