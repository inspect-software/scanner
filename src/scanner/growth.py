"""Growth authenticity: does the star and fork history look organic?

Stars are the most widely read trust signal in open source, and the only one
with no issuer — they are bought openly, in bulk, for a few cents each. A
report that treats a purchased star exactly like an earned one is repeating a
claim it has not checked.

This module reads the per-day star and fork history already collected for the
history chart (``Popularity.star_history`` / ``fork_history``) and reports
whether the shape of that growth is consistent with organic accretion. It
describes a **pattern**, never an intent: nothing here establishes that
attention was purchased, or that a maintainer was involved if it was.

The assessment is deliberately conservative, because a false accusation costs
far more than a missed one:

- A burst alone is never a finding. Real projects trend on Hacker News, ship a
  1.0, or get mentioned in a newsletter, and all of those look like a spike.
  A window is confirmed only when the burst is accompanied by **at least two
  independent corroborating signals**.
- Anything the collected window cannot answer is ``unverified``, which carries
  no penalty at all. History collection is bounded (see ``collect.py``), so
  quiet or truncated evidence is the normal case, not a suspicious one.

The result is a multiplier over the star and fork components of
``popularity`` — the inflated inputs are discounted rather than scored beside
a new component, the same shape as the human-authorship factor in
``metrics.py`` and the jurisdiction multiplier before it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Optional

from .models import ForkHistory, RepoData, StarDay, StarHistory

# Below this many stars, a single day's additions are noise: a repository that
# gained 40 stars in its lifetime cannot be said to have a baseline at all.
MIN_STARS_ASSESSED = 100

# The collected window must span at least this long. History is capped at a
# fixed number of star events, so a fast-growing repository's window can cover
# only days — far too short to tell a burst from the norm.
MIN_SPAN_DAYS = 60

# A spike day must clear both bars: an absolute floor, so small repositories
# are not judged on single-digit noise, and a multiple of the repository's own
# baseline, so a busy project is measured against itself.
SPIKE_MIN_STARS_PER_DAY = 25
SPIKE_BASELINE_MULTIPLE = 12

# Spike days this far apart still belong to the same event.
SPIKE_MERGE_GAP_DAYS = 1

# Flat cadence: a coefficient of variation this low across a multi-day window
# is a delivery schedule, not an audience. Organic attention is spiky by the
# hour and by the day; a supplier drips a fixed quota.
FLAT_CADENCE_MIN_DAYS = 3
FLAT_CADENCE_MAX_CV = 0.25

# Fork response: genuine attention forks as well as stars. A window whose
# fork-to-star ratio falls this far below the repository's own long-run ratio
# gained watchers who never touched the code.
FORK_RESPONSE_SHARE = 0.25
# Below this long-run ratio the comparison is meaningless (a repo nobody forks
# cannot show a missing fork response).
FORK_RATIO_FLOOR = 0.02

# Concentration: the share of all collected stars that arrived on the busiest
# few days. A repository where nearly everything arrived in under a week, and
# nothing happened on the other several hundred days, is making an
# extraordinary claim about how attention reached it.
#
# Measured over 502 ordinary repositories in the 100-1,500 star band: the
# median puts 6.9% of stars in its five busiest days, and *none* reached 80%.
# The threshold sits at the top of that observed range rather than at a round
# number chosen in advance. A legitimate announcement-driven release does
# approach it — a research model drop measured 73.7% — which is exactly why it
# corroborates a burst rather than standing as a finding on its own.
CONCENTRATION_TOP_DAYS = 5
CONCENTRATION_SHARE = 0.80

# Missing decay: real spikes have a tail — the following week still runs above
# the norm as the link circulates. A burst that stops dead was switched off.
DECAY_TAIL_DAYS = 7
DECAY_TAIL_SHARE = 0.05

# How many corroborating signals confirm a window, and how many make it severe.
CONFIRM_SIGNALS = 2
SEVERE_SIGNALS = 3

# Discount applied to the star and fork components of popularity.
STATE_FACTOR: dict[str, float] = {
    "organic": 1.0,
    "unverified": 1.0,
    "anomalous": 0.6,
    "highly_anomalous": 0.3,
}

GrowthState = Literal["organic", "unverified", "anomalous", "highly_anomalous"]
SignalKey = Literal[
    "acquisition_burst",
    "star_concentration",
    "flat_cadence",
    "fork_divergence",
    "missing_decay",
    "pre_substance_spike",
]


@dataclass
class GrowthWindow:
    """One burst of star additions, with whatever corroborates it."""

    start: date
    end: date
    stars: int
    peak: int
    multiple: float
    corroborating: list[SignalKey] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return len(self.corroborating) >= CONFIRM_SIGNALS

    @property
    def severe(self) -> bool:
        return len(self.corroborating) >= SEVERE_SIGNALS

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def label(self) -> str:
        if self.start == self.end:
            return self.start.isoformat()
        return f"{self.start.isoformat()} → {self.end.isoformat()}"


@dataclass
class GrowthAssessment:
    """What the history says, and what the score should do about it."""

    state: GrowthState
    factor: float
    reason: Optional[str] = None
    signals: list[SignalKey] = field(default_factory=list)
    windows: list[GrowthWindow] = field(default_factory=list)
    span_days: int = 0
    baseline_per_day: float = 0.0
    top_days_share: float = 0.0
    history_complete: bool = False

    @property
    def flagged(self) -> bool:
        return self.state in ("anomalous", "highly_anomalous")

    @property
    def peak_window(self) -> Optional[GrowthWindow]:
        """The confirmed window with the most stars — the one worth showing."""
        confirmed = [w for w in self.windows if w.confirmed]
        return max(confirmed, key=lambda w: w.stars, default=None)


def _unverified(reason: str, **extra) -> GrowthAssessment:
    return GrowthAssessment(state="unverified", factor=1.0, reason=reason, **extra)


def _dense(days: list[StarDay]) -> tuple[list[date], list[int]]:
    """Expand a sparse day list into one entry per calendar day, zeros included.

    The collected history lists only days that saw activity; a baseline read
    off that list alone would describe the active days of a quiet project as
    its normal rate.
    """
    if not days:
        return [], []
    counts = {date.fromisoformat(d.date): d.count for d in days}
    first, last = min(counts), max(counts)
    dates = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return dates, [counts.get(day, 0) for day in dates]


def _baseline(series: list[int]) -> float:
    """The repository's ordinary daily rate.

    Median over the active days: a median cannot be moved by the handful of
    days a burst occupies, which is exactly the property needed when the
    burst is what is being measured against it.
    """
    active = [n for n in series if n > 0]
    if not active:
        return 1.0
    return max(1.0, float(statistics.median(active)))


def _spike_windows(dates: list[date], series: list[int], baseline: float) -> list[GrowthWindow]:
    threshold = max(float(SPIKE_MIN_STARS_PER_DAY), SPIKE_BASELINE_MULTIPLE * baseline)
    windows: list[GrowthWindow] = []
    run: list[int] = []  # indices of the current window

    def close(run: list[int]) -> None:
        if not run:
            return
        chunk = series[run[0] : run[-1] + 1]
        windows.append(
            GrowthWindow(
                start=dates[run[0]],
                end=dates[run[-1]],
                stars=sum(chunk),
                peak=max(chunk),
                multiple=round(max(chunk) / baseline, 1),
            )
        )

    for i, count in enumerate(series):
        if count >= threshold:
            if run and i - run[-1] > SPIKE_MERGE_GAP_DAYS + 1:
                close(run)
                run = []
            run.append(i)
    close(run)
    return windows


def _concentration(series: list[int]) -> float:
    """Share of collected stars that arrived on the busiest few days."""
    total = sum(series)
    if total <= 0:
        return 0.0
    busiest = sorted(series, reverse=True)[:CONCENTRATION_TOP_DAYS]
    return round(sum(busiest) / total, 3)


def _flat_cadence(series: list[int], dates: list[date], window: GrowthWindow) -> bool:
    lo = dates.index(window.start)
    hi = dates.index(window.end)
    chunk = [n for n in series[lo : hi + 1] if n > 0]
    if len(chunk) < FLAT_CADENCE_MIN_DAYS:
        return False
    mean = statistics.mean(chunk)
    if mean <= 0:
        return False
    return statistics.pstdev(chunk) / mean < FLAT_CADENCE_MAX_CV


def _missing_decay(series: list[int], dates: list[date], window: GrowthWindow) -> Optional[bool]:
    """True when nothing followed the burst. None when the tail is unobserved."""
    hi = dates.index(window.end)
    if hi + DECAY_TAIL_DAYS >= len(series):
        return None
    tail = sum(series[hi + 1 : hi + 1 + DECAY_TAIL_DAYS])
    return tail <= DECAY_TAIL_SHARE * window.stars


def _fork_divergence(
    forks: Optional[ForkHistory], stars: StarHistory, window: GrowthWindow
) -> Optional[bool]:
    """True when the burst brought no forks. None when forks cannot be compared."""
    if forks is None or not forks.days or not stars.total_stars:
        return None
    long_run = forks.total_forks / stars.total_stars
    if long_run < FORK_RATIO_FLOOR:
        return None
    fork_dates, fork_series = _dense(forks.days)
    if not fork_dates or fork_dates[0] > window.start or fork_dates[-1] < window.end:
        return None
    lo = fork_dates.index(window.start)
    hi = fork_dates.index(window.end)
    in_window = sum(fork_series[lo : hi + 1])
    return (in_window / window.stars) < FORK_RESPONSE_SHARE * long_run


def _pre_substance(data: RepoData, window: GrowthWindow) -> Optional[bool]:
    """True when the burst preceded anything the project had shipped."""
    a = data.activity
    if a.releases_count is None:
        return None
    if a.releases_count == 0:
        return True
    # The release list is capped at the newest 100; beyond that the earliest
    # entry is not the first release and the comparison would be wrong.
    if a.releases_count > len(a.releases):
        return None
    dated = [r.published_at for r in a.releases if r.published_at is not None]
    if not dated:
        return None
    return min(dated).date() > window.end


def assess(data: RepoData) -> GrowthAssessment:
    """Assess the authenticity of a repository's popularity growth."""
    stars = data.popularity.star_history
    if stars is None or not stars.days:
        return _unverified("no_history")
    if stars.total_stars < MIN_STARS_ASSESSED:
        return _unverified("below_threshold", history_complete=stars.complete)

    dates, series = _dense(stars.days)
    span = (dates[-1] - dates[0]).days + 1
    if span < MIN_SPAN_DAYS:
        return _unverified("window_too_short", span_days=span, history_complete=stars.complete)

    baseline = _baseline(series)
    windows = _spike_windows(dates, series, baseline)
    # Repo-level, so it corroborates every window rather than one of them: the
    # observation is about the history as a whole, not about a single burst.
    concentration = _concentration(series)
    # Only when the whole history was collected. The signal's claim is that
    # nothing happened on all the *other* days, and a truncated window has not
    # seen them: a repository whose collected slice happens to open mid-burst
    # would read as concentrated on evidence that does not exist.
    concentrated = stars.complete and concentration >= CONCENTRATION_SHARE
    for window in windows:
        checks: list[tuple[SignalKey, Optional[bool]]] = [
            ("star_concentration", concentrated),
            ("flat_cadence", _flat_cadence(series, dates, window)),
            ("fork_divergence", _fork_divergence(data.popularity.fork_history, stars, window)),
            ("missing_decay", _missing_decay(series, dates, window)),
            ("pre_substance_spike", _pre_substance(data, window)),
        ]
        window.corroborating = [key for key, hit in checks if hit]

    confirmed = [w for w in windows if w.confirmed]
    if not confirmed:
        state: GrowthState = "organic"
    elif len(confirmed) > 1 or any(w.severe for w in confirmed):
        state = "highly_anomalous"
    else:
        state = "anomalous"

    signals: list[SignalKey] = []
    if confirmed:
        signals.append("acquisition_burst")
        for window in confirmed:
            for key in window.corroborating:
                if key not in signals:
                    signals.append(key)

    return GrowthAssessment(
        state=state,
        factor=STATE_FACTOR[state],
        signals=signals,
        windows=windows,
        span_days=span,
        baseline_per_day=baseline,
        top_days_share=concentration,
        history_complete=stars.complete,
    )
