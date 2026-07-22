"""The red-flag registry: one declaration, read the same way by every consumer."""

import pytest

from scanner import redflags
from scanner.models import Metric, MetricCategory, Metrics


def metrics(*entries: tuple[str, dict]) -> Metrics:
    return Metrics(
        metrics_version="1.13.0",
        categories=[
            MetricCategory(
                key="scope",
                name="Scope",
                description="",
                weight=1.0,
                metrics=[
                    Metric(key=key, name=key, value=50, band="moderate", inputs=inputs)
                    for key, inputs in entries
                ],
            )
        ],
    )


def verdicts(m) -> dict[str, tuple[bool, bool]]:
    return {f.flag.key: (f.assessed, f.flagged) for f in redflags.assess(m)}


def test_every_declared_flag_is_reported_even_when_its_metric_is_absent():
    """A caller must be able to tell 'no finding' from 'no evidence'."""
    result = verdicts(metrics())
    assert set(result) == set(redflags.BY_KEY)
    assert all(verdict == (False, False) for verdict in result.values())


@pytest.mark.parametrize("value,expected", [(True, (True, True)), (False, (True, False))])
def test_a_metric_carrying_red_flag_is_assessed_either_way(value, expected):
    result = verdicts(metrics(("abandonment", {"red_flag": value})))
    assert result["abandonment"] == expected


def test_growth_reads_its_state_and_unverified_is_not_clean():
    assert verdicts(metrics(("popularity", {"growth_state": "organic"})))["inorganic_growth"] == (True, False)
    assert verdicts(metrics(("popularity", {"growth_state": "anomalous"})))["inorganic_growth"] == (True, True)
    assert verdicts(metrics(("popularity", {"growth_state": "highly_anomalous"})))["inorganic_growth"] == (True, True)
    # The point of the four-state field: unanswerable is neither a finding
    # nor a pass, so it must not be assessed.
    assert verdicts(metrics(("popularity", {"growth_state": "unverified"})))["inorganic_growth"] == (False, False)
    assert verdicts(metrics(("popularity", {"stars": 10})))["inorganic_growth"] == (False, False)


def test_flagged_keys_summarizes_a_report_in_registry_order():
    m = metrics(
        ("abandonment", {"red_flag": True}),
        ("malicious_dependencies", {"red_flag": False}),
        ("high_risk_jurisdiction_exposure", {"red_flag": True}),
        ("popularity", {"growth_state": "highly_anomalous"}),
    )
    assert redflags.flagged_keys(m) == [
        "abandonment",
        "high_risk_jurisdiction_exposure",
        "inorganic_growth",
    ]


def test_a_report_with_no_metrics_at_all_raises_nothing():
    assert redflags.flagged_keys(None) == []
    assert redflags.flagged_keys(metrics()) == []


def test_every_registry_entry_is_declared_completely():
    """A half-declared flag is the failure this registry exists to prevent."""
    for flag in redflags.REGISTRY:
        assert flag.key and flag.metric_key and flag.name and flag.wiki_slug
        assert callable(flag.read)
    assert len({f.key for f in redflags.REGISTRY}) == len(redflags.REGISTRY)
