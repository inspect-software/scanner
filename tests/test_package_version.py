"""``__version__`` and the ``pyproject.toml`` version are the same string.

Two places state the package version and nothing compared them, so they drifted:
``pyproject.toml`` reached 0.10.0 while ``scanner.__version__`` still said 0.9.0.
Neither is read by the scoring path — reports carry ``SCHEMA_VERSION`` and
``METRICS_VERSION`` instead — which is exactly why the mismatch survived: a
wrong answer here breaks nothing that runs, and only misleads whoever asks the
installed package what it is.

That question starts to matter once the scanner is distributed on its own, so
the two are pinned together here rather than left to review.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import scanner

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_dunder_version_matches_pyproject() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert scanner.__version__ == declared, (
        f"scanner.__version__ is {scanner.__version__!r} but pyproject.toml "
        f"declares {declared!r}. Bump both, or the installed package reports a "
        f"version that was never released."
    )
