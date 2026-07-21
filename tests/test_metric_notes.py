"""Component keys and machine-identified metric notes.

A metric's ``note`` is generated English. Localized surfaces cannot translate
generated text, so every statement is also reported as a code with its values,
and every component carries a stable key. These tests pin both, and pin that
the English prose did not change when the codes were added.
"""

from scanner.metrics import _slug, compute_metrics, metric_popularity
from scanner.models import (
    Popularity,
    RepoData,
    ScanConfig,
    SecuritySignals,
)


def codes(metric):
    return [n.code for n in metric.notes]


def test_component_key_is_a_stable_slug_of_the_name():
    assert _slug("OpenSSF Scorecard: Signed-Releases") == "openssf_scorecard_signed_releases"
    assert _slug(".editorconfig") == "editorconfig"
    assert _slug("Machine-readable docs (llms.txt)") == "machine_readable_docs_llms_txt"


def test_every_component_carries_a_key():
    metric = metric_popularity(RepoData(popularity=Popularity(stars=500, forks=40, watchers=20)))
    assert metric is not None
    assert [c.key for c in metric.components] == ["stars", "forks", "watchers"]


def test_excluded_components_are_reported_as_codes_and_keys():
    # No dependency manifests: the lockfile component is excluded, and the
    # file-tree fallback runs because no Scorecard is present.
    data = RepoData(security_signals=SecuritySignals())
    metrics = compute_metrics(data)
    posture = next(
        m
        for c in metrics.categories
        for m in c.metrics
        if m.key == "security_posture"
    )
    assert "excluded_no_data" in codes(posture)
    assert "weights_renormalized" in codes(posture)
    excluded = next(n for n in posture.notes if n.code == "excluded_no_data")
    assert "dependency_lockfiles" in excluded.params["components"]
    # The prose is unchanged — consumers that read it still see the same text.
    assert "Excluded from scoring" in posture.note
    assert "Remaining weights renormalized." in posture.note


def test_configuration_disabled_components_carry_their_own_code():
    data = RepoData(popularity=Popularity(stars=500, forks=40, watchers=20))
    config = ScanConfig(disabled_components={"popularity": ["Watchers"]})
    metrics = compute_metrics(data, config)
    popularity = next(
        m for c in metrics.categories for m in c.metrics if m.key == "popularity"
    )
    assert "disabled_in_config" in codes(popularity)
    disabled = next(n for n in popularity.notes if n.code == "disabled_in_config")
    assert disabled.params["components"] == ["watchers"]


def test_a_clean_metric_has_neither_note_nor_codes():
    metric = metric_popularity(RepoData(popularity=Popularity(stars=500, forks=40, watchers=20)))
    assert metric is not None
    assert metric.note is None
    assert metric.notes == []
