from __future__ import annotations

import pytest

import scanner.github as github_module
from scanner.github import GitHubClient


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    """Make every GitHub retry pause instant, and advance a fake clock by it.

    Autouse and suite-wide on purpose. The client's retry budget is wall-clock
    (RETRY_BUDGET_SECONDS), so a pause that takes no time *and* leaves the clock
    where it was lets the retry loop spin against a mock transport until five
    real minutes have elapsed — which is exactly what one unpatched test in
    test_sbom.py did, turning a 9-second suite into a 4½-minute one. Advancing
    the clock keeps the budget's semantics while costing nothing.

    Yields the list of pauses, so a test can assert on what was waited for.
    """
    naps: list[float] = []
    clock = [1000.0]

    def fake_sleep(seconds: float) -> None:
        naps.append(seconds)
        clock[0] += max(seconds, 0.0)

    monkeypatch.setattr(GitHubClient, "_sleep", staticmethod(fake_sleep))
    monkeypatch.setattr(github_module.time, "monotonic", lambda: clock[0])
    return naps
