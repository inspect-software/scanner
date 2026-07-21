"""Growth authenticity: the anomaly detector and its effect on popularity."""

from datetime import date, datetime, timedelta, timezone

from scanner.growth import (
    MIN_SPAN_DAYS,
    MIN_STARS_ASSESSED,
    assess,
)
from scanner.metrics import metric_popularity
from scanner.models import (
    Activity,
    ForkDay,
    ForkHistory,
    Popularity,
    RepoData,
    ReleaseRecord,
    StarDay,
    StarHistory,
)

START = date(2025, 1, 1)


def days(counts: dict[int, int], span: int = 400) -> list[StarDay]:
    """Star days from {day offset: count}, over a span of `span` days."""
    return [
        StarDay(date=(START + timedelta(days=offset)).isoformat(), count=count)
        for offset, count in sorted(counts.items())
        if count > 0
    ]


def fork_days(counts: dict[int, int]) -> list[ForkDay]:
    return [
        ForkDay(date=(START + timedelta(days=offset)).isoformat(), count=count)
        for offset, count in sorted(counts.items())
        if count > 0
    ]


def steady(total_days: int = 365, per_day: int = 2) -> dict[int, int]:
    """An ordinary trickle: a couple of stars most days."""
    return {i: per_day for i in range(total_days) if i % 3}


def repo(
    star_counts: dict[int, int],
    *,
    total_stars: int | None = None,
    fork_counts: dict[int, int] | None = None,
    total_forks: int = 200,
    releases: list[ReleaseRecord] | None = None,
    releases_count: int | None = None,
    complete: bool = True,
) -> RepoData:
    sd = days(star_counts)
    stars = total_stars if total_stars is not None else sum(star_counts.values())
    history = StarHistory(
        total_stars=stars, collected=sum(d.count for d in sd), complete=complete, days=sd
    )
    forks = None
    if fork_counts is not None:
        fd = fork_days(fork_counts)
        forks = ForkHistory(
            total_forks=total_forks,
            collected=sum(d.count for d in fd),
            complete=True,
            days=fd,
        )
    rels = releases if releases is not None else []
    return RepoData(
        popularity=Popularity(
            stars=stars, forks=total_forks, watchers=50, star_history=history, fork_history=forks
        ),
        activity=Activity(
            releases=rels,
            releases_count=releases_count if releases_count is not None else len(rels),
        ),
    )


def release_on(offset: int) -> ReleaseRecord:
    return ReleaseRecord(
        tag="v1.0.0",
        published_at=datetime.combine(START + timedelta(days=offset), datetime.min.time()).replace(
            tzinfo=timezone.utc
        ),
        kind="major",
    )


# --------------------------------------------------------------------------
# Nothing to report
# --------------------------------------------------------------------------


def test_no_history_is_unverified_and_costs_nothing():
    data = RepoData(popularity=Popularity(stars=5000, forks=300, watchers=90))
    result = assess(data)
    assert result.state == "unverified"
    assert result.reason == "no_history"
    assert result.factor == 1.0


def test_small_repository_is_not_assessed():
    counts = {i: 1 for i in range(90)}
    result = assess(repo(counts, total_stars=MIN_STARS_ASSESSED - 1))
    assert result.state == "unverified"
    assert result.reason == "below_threshold"


def test_short_window_is_unverified_not_organic():
    """A fast-growing repo's collected window covers days, not months."""
    counts = {i: 200 for i in range(MIN_SPAN_DAYS - 20)}
    result = assess(repo(counts))
    assert result.state == "unverified"
    assert result.reason == "window_too_short"


def test_steady_growth_is_organic():
    result = assess(repo(steady(), fork_counts={i: 1 for i in range(0, 365, 5)}))
    assert result.state == "organic"
    assert result.factor == 1.0
    assert result.signals == []


def test_organic_spike_with_a_tail_and_forks_is_not_flagged():
    """A launch: one big day, a decaying tail, and forks that follow."""
    counts = steady()
    counts[200] = 600
    for i, tail in enumerate([220, 140, 90, 60, 40, 30, 20], start=201):
        counts[i] = tail
    forks = {i: 1 for i in range(0, 365, 5)}
    forks[200] = 60
    forks[201] = 25
    result = assess(repo(counts, fork_counts=forks, releases=[release_on(10)]))
    assert result.state == "organic"


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


def test_flat_burst_without_forks_or_decay_is_anomalous():
    counts = steady()
    for offset in (200, 201, 202, 203):
        counts[offset] = 300  # identical daily quota, then nothing
    forks = {i: 1 for i in range(0, 365, 5)}
    result = assess(repo(counts, fork_counts=forks, releases=[release_on(10)]))

    assert result.state in ("anomalous", "highly_anomalous")
    assert "acquisition_burst" in result.signals
    assert "flat_cadence" in result.signals
    assert "fork_divergence" in result.signals
    assert "missing_decay" in result.signals
    window = result.peak_window
    assert window is not None
    assert window.stars == 1200
    assert window.days == 4


def test_two_confirmed_windows_read_as_highly_anomalous():
    counts = steady()
    for offset in (150, 151, 152, 300, 301, 302):
        counts[offset] = 300
    forks = {i: 1 for i in range(0, 365, 5)}
    result = assess(repo(counts, fork_counts=forks, releases=[release_on(10)]))
    assert result.state == "highly_anomalous"
    assert result.factor == 0.3


def test_a_burst_alone_is_never_a_finding():
    """Corroboration is required: an unobservable tail must not confirm."""
    counts = steady()
    counts[200] = 900  # single day, so no cadence signal
    forks = {i: 1 for i in range(0, 365, 5)}
    forks[200] = 40  # forks responded
    result = assess(
        repo(counts, fork_counts=forks, releases=[release_on(10)])
    )
    assert result.state == "organic"
    assert result.windows and not result.windows[0].confirmed


def test_unobserved_tail_does_not_count_as_missing_decay():
    """A burst at the very end of the window has no observable aftermath."""
    counts = steady(total_days=300)
    for offset in (297, 298, 299):
        counts[offset] = 300
    result = assess(repo(counts, fork_counts={i: 1 for i in range(0, 300, 5)}))
    window = result.windows[-1]
    assert "missing_decay" not in window.corroborating


# --------------------------------------------------------------------------
# Effect on the score
# --------------------------------------------------------------------------


def test_popularity_discounts_stars_and_forks_but_not_watchers():
    counts = steady()
    for offset in (200, 201, 202, 203):
        counts[offset] = 300
    flagged = metric_popularity(repo(counts, fork_counts={i: 1 for i in range(0, 365, 5)}))
    clean = metric_popularity(repo(steady(), fork_counts={i: 1 for i in range(0, 365, 5)}))

    assert flagged is not None and clean is not None
    assert flagged.value < clean.value
    by_name = {c.name: c for c in flagged.components}
    clean_by_name = {c.name: c for c in clean.components}
    assert by_name["Watchers"].points == clean_by_name["Watchers"].points
    assert by_name["Stars"].points < clean_by_name["Stars"].points
    assert by_name["Forks"].points < clean_by_name["Forks"].points
    assert flagged.inputs["growth_state"] in ("anomalous", "highly_anomalous")
    assert "acquisition_burst" in flagged.inputs["growth_signals"]
    # The chart marks these days, so the interval must be machine-readable.
    assert flagged.inputs["growth_windows"] == ["2025-07-20/2025-07-23"]
    assert flagged.note and "Inorganic Growth Policy" in flagged.note


def test_clean_repository_carries_the_state_without_a_note():
    metric = metric_popularity(repo(steady(), fork_counts={i: 1 for i in range(0, 365, 5)}))
    assert metric is not None
    assert metric.inputs["growth_state"] == "organic"
    assert metric.inputs["growth_factor_pct"] == 100
    assert "growth_signals" not in metric.inputs
    assert metric.note is None


def test_missing_history_leaves_popularity_untouched():
    data = RepoData(popularity=Popularity(stars=5000, forks=300, watchers=90))
    metric = metric_popularity(data)
    assert metric is not None
    assert metric.inputs["growth_state"] == "unverified"
    assert metric.inputs["growth_unverified_reason"] == "no_history"
    assert metric.note is None
