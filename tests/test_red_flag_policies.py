"""Red flags do not compound: the strictest of those that fired governs alone.

Before 1.12.0 each policy multiplied whatever the previous one left, so a
repository carrying two landed on the product of both. Measured live on
svaarala/duktape: a weighted 50 became 18 under the malicious-dependency
multiplier and then 11 under abandonment — a number no policy chose, and none
could be pointed at to explain.
"""

from __future__ import annotations

from scanner.metrics import (
    HIGH_RISK_JURISDICTION_OVERALL_CAP,
    MALICIOUS_DEPENDENCY_MULTIPLIER,
    compute_metrics,
)
from scanner.models import (
    AllDependencies,
    DependencyAdvisories,
    MaliciousDependency,
    OwnerProfile,
    RepoData,
)


def _malware() -> DependencyAdvisories:
    return DependencyAdvisories(
        collected=True,
        source="osv",
        scope="repository_graph",
        assessed_count=200,
        malicious_count=1,
        malicious=[
            MaliciousDependency(
                ecosystem="npm", name="evil", version="1.0.0", direct=True,
                advisory_ids=["MAL-1"], still_published=True,
            )
        ],
    )


def _data(*, malware: bool = False, jurisdiction: bool = False) -> RepoData:
    data = RepoData()
    data.dependencies.all_dependencies = AllDependencies(collected=True)
    if malware:
        data.dependencies.advisories = _malware()
    else:
        data.dependencies.advisories = DependencyAdvisories(
            collected=True, source="osv", scope="repository_graph", assessed_count=200
        )
    if jurisdiction:
        # A self-published owner location inside the policy scope.
        data.owner = OwnerProfile(login="acme", type="User", location="Moscow, Russia")
    return data


def _overall(data: RepoData):
    metrics = compute_metrics(data)
    assert metrics.overall is not None
    return metrics.overall


def test_one_policy_applies_normally():
    overall = _overall(_data(malware=True))
    assert overall.inputs["malicious_dependency_multiplier"] == MALICIOUS_DEPENDENCY_MULTIPLIER
    assert "high_risk_jurisdiction_multiplier" not in overall.inputs


def test_two_policies_do_not_multiply_together():
    both = _overall(_data(malware=True, jurisdiction=True))
    # Exactly one policy wrote its adjustment onto the score.
    applied = [
        key
        for key in ("malicious_dependency_multiplier", "high_risk_jurisdiction_multiplier")
        if key in both.inputs
    ]
    assert len(applied) == 1


def test_the_strictest_policy_is_the_one_that_governs():
    """Owner-role jurisdiction exposure multiplies by 20, malware by 35, so the
    jurisdiction reading is the harsher of the two and must win."""
    both = _overall(_data(malware=True, jurisdiction=True))
    assert both.inputs["high_risk_jurisdiction_multiplier"] == 20
    assert both.inputs["high_risk_jurisdiction_cap"] == HIGH_RISK_JURISDICTION_OVERALL_CAP
    assert "malicious_dependency_multiplier" not in both.inputs


def test_carrying_two_findings_is_never_worse_than_the_worst_one_alone():
    """The property the old behaviour broke."""
    worst_alone = min(
        _overall(_data(malware=True)).value,
        _overall(_data(jurisdiction=True)).value,
    )
    assert _overall(_data(malware=True, jurisdiction=True)).value == worst_alone


def test_a_clean_repository_is_untouched_by_either_policy():
    overall = _overall(_data())
    assert "malicious_dependency_multiplier" not in overall.inputs
    assert "high_risk_jurisdiction_multiplier" not in overall.inputs
