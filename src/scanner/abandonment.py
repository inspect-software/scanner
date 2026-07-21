"""Abandonment: has anyone been left holding this project?

Every tool in this space answers the question with a single number — days
since the last commit — and every one of them is wrong about the same
projects. A small, complete C library that has not needed a commit in three
years is not abandoned; it is finished. Marking it as dead costs the report
its credibility precisely on the software that deserves the most confidence.

So silence alone is never a finding here. The assessment rests on a
distinction that turns out to do all the work:

    Abandonment is an unmet obligation, not an absence of noise.

A project incurs obligations when work arrives from outside — a pull request
that wants review, an issue that wants an answer, a published fix for a
vulnerability in something it ships. A quiet project with no such demands owes
nothing and cannot be failing anyone. A quiet project with fifteen pull
requests nobody has looked at in two years, and a year-old advisory whose
patch was released the same week, is not resting. That is the difference this
module measures, and it is measurable entirely from facts the scan already
collects.

The evidence is tiered:

- **Declared** — the maintainer said so. GitHub's archive flag, or a registry
  deprecation on every package the repository publishes. Nothing here is
  inferred and nothing needs corroborating; the report is quoting its subject.
- **Drought** — no *human* commit for a long time. Deliberately not
  ``days_since_last_push``, which counts a Dependabot bump as a sign of life:
  measured, repositories whose only activity for a year was automated version
  bumps report a push recency of days. Drought is a necessary condition, never
  a finding on its own.
- **Unmet obligations** — the corroborating signals, listed in ``Signal``.
  Each one is a duty the project is visibly not discharging.
- **Guards** — evidence of a maintainer still present, or of nothing being
  asked of them. Two guards hold the result at ``dormant`` no matter how many
  obligation signals fired, because both readings ("someone is answering" and
  "nobody is asking") are complete explanations of the silence.

Like the growth assessment, anything the collected data cannot answer is
``unverified`` and costs the repository nothing. An unauthenticated scan reads
no commit sample and no tracker queues, so it can reach no conclusion; that is
recorded as an absence of evidence rather than evidence of absence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from .models import RepoData

# No human commit for this long is a drought. Six months is the point at which
# a project's own contributors have stopped touching it across a full release
# cycle for every cadence the corpus contains; below it, ordinary summers and
# between-release lulls dominate and the signal is noise.
DROUGHT_SOFT_DAYS = 180
# A year of human silence is the hard bar. Every state above ``dormant``
# requires it, or two guards' worth of demand with the soft bar.
DROUGHT_HARD_DAYS = 365

# A repository younger than this has no drought to measure — its whole life is
# shorter than the window.
MIN_AGE_DAYS = 180

# Open pull requests older than this are unanswered rather than in progress.
STALE_PR_DAYS = 180
# Open issues older than this with no maintainer reply have been abandoned by
# the tracker, not merely left open. A year is deliberately generous: plenty of
# healthy projects keep long-lived feature requests open on purpose.
STALE_ISSUE_DAYS = 365

# How many unanswered items make a queue unattended rather than merely long.
UNANSWERED_PRS_MIN = 3
ROTTEN_ISSUES_MIN = 5

# An advisory public this long, on a direct dependency, with a fixed version
# already published, is a one-line change nobody has made.
UNFIXED_ADVISORY_DAYS = 365

# A release gap this many times the project's own mean cadence is a stall
# measured against itself — the same reasoning as the growth baseline. The
# absolute floor keeps a project that releases weekly from being called
# stalled after two quiet months.
RELEASE_STALL_MULTIPLE = 4
RELEASE_STALL_FLOOR_DAYS = 365

# A maintainer reply this recent means someone is still holding the project,
# whether or not they are writing code. Answering "works as intended, closing"
# is maintenance.
MAINTAINER_REPLY_DAYS = 180
# Below this much open work, nothing is being asked of the project.
NO_DEMAND_MAX_OPEN_ISSUES = 3

# How many obligation signals confirm a state.
AT_RISK_SIGNALS = 2
ABANDONED_SIGNALS = 3
# Guards this many or more hold the result at dormant.
SUPPRESSING_GUARDS = 2

State = Literal[
    "maintained",
    "unverified",
    "dormant",
    "at_risk",
    "likely_abandoned",
    "declared",
]

Signal = Literal[
    "unanswered_contributions",
    "issue_rot",
    "unfixed_advisory",
    "release_stall",
    "scorecard_unmaintained",
    "sole_maintainer_gone",
    "broken_ci",
]

Guard = Literal[
    "maintainer_replying",
    "no_open_demand",
    "recent_release",
    "dependencies_clean",
]

DeclaredReason = Literal["archived", "packages_deprecated"]

UnverifiedReason = Literal["no_commit_sample", "repository_too_young", "queues_not_read"]

# Multiplier applied to the weighted overall score, as a percentage, and the
# ceiling that comes with it. ``dormant`` is deliberately 100: a quiet project
# with no unmet obligations is a fact worth showing and not a fault worth
# pricing. The scored penalty for silence already exists inside
# ``development_activity``; these multipliers exist for a different reason —
# when a project is genuinely abandoned, the rest of the report stops meaning
# what it says. Good documentation on a dead project is not good documentation.
STATE_FACTOR: dict[str, int] = {
    "maintained": 100,
    "unverified": 100,
    "dormant": 100,
    "at_risk": 85,
    "likely_abandoned": 60,
    "declared": 40,
}
# No ceiling below ``likely_abandoned``: at_risk is a warning, not a verdict.
STATE_CAP: dict[str, Optional[int]] = {
    "maintained": None,
    "unverified": None,
    "dormant": None,
    "at_risk": None,
    "likely_abandoned": 49,
    "declared": 29,
}


@dataclass
class AbandonmentAssessment:
    """What the maintenance record says, and what the score should do about it."""

    state: State
    factor: int
    cap: Optional[int] = None
    signals: list[Signal] = field(default_factory=list)
    guards: list[Guard] = field(default_factory=list)
    declared_reason: Optional[DeclaredReason] = None
    unverified_reason: Optional[UnverifiedReason] = None
    drought_days: Optional[int] = None
    drought_is_floor: bool = False
    unanswered_prs: Optional[int] = None
    rotten_issues: Optional[int] = None
    days_since_last_merged_pr: Optional[int] = None

    @property
    def flagged(self) -> bool:
        """Whether the state carries a multiplier — the site's red flag."""
        return self.state in ("at_risk", "likely_abandoned", "declared")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(moment: Optional[datetime]) -> Optional[int]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (_now() - moment).days


def _drought(data: RepoData) -> tuple[Optional[int], bool]:
    """Days since the last human commit, and whether that is only a floor.

    Returns ``(None, False)`` when no commit sample was collected. When the
    whole sampled window is automation, the true figure is older than anything
    observed, so the age of the oldest sampled commit is returned as a floor —
    understating a drought is the safe direction.
    """
    commits = data.activity.recent_commits
    if not commits:
        return None, False
    for commit in commits:  # newest first
        if not commit.is_bot:
            return _days_since(commit.committed_at), False
    return _days_since(commits[-1].committed_at), True


def _maintainer_logins(data: RepoData) -> set[str]:
    """Accounts whose reply counts as the project answering.

    The top contributors plus the owning account. Case-folded: GitHub logins
    are case-insensitive and the two endpoints do not agree on casing.
    """
    logins = {c.login.lower() for c in data.maintainership.top_contributors if c.login}
    if data.owner and data.owner.login:
        logins.add(data.owner.login.lower())
    return logins


def _declared(data: RepoData) -> Optional[DeclaredReason]:
    """The maintainer's own statement that the project is not maintained."""
    if data.repo.is_archived:
        return "archived"
    # Only packages the registry links back to this repository can speak for
    # it, and only when *every* one of them is deprecated: a project that
    # publishes five packages and has retired one has retired a package, not
    # itself.
    #
    # `matches_repo is True`, not `is not False`. A registry entry that
    # declares no repository at all (None) is the shape a squatted name takes,
    # and a squatted name must never be allowed to speak for a project.
    # Measured: PyPI `ComfyUI` is a placeholder at 0.0.1 with no declared
    # repository, and under the looser test it declared comfyanonymous/ComfyUI
    # — among the most actively developed projects in its field — abandoned,
    # taking it from 68 to 27.
    owned = [p for p in data.ecosystem.packages if p.matches_repo is True]
    # Deprecation only. A yanked *latest version* is a withdrawn release,
    # usually a bad build with a fix right behind it — a release-quality
    # signal, and `package_maintenance` is where it costs points. It is not
    # the maintainer saying the project is over, which is the only thing this
    # tier is allowed to assert.
    if owned and all(p.is_deprecated for p in owned):
        return "packages_deprecated"
    return None


def _unanswered_prs(data: RepoData) -> int:
    cutoff = STALE_PR_DAYS
    return sum(
        1
        for pr in data.contribution_flow.oldest_open_prs
        if (_days_since(pr.created_at) or 0) > cutoff
    )


def _rotten_issues(data: RepoData, maintainers: set[str]) -> int:
    """Long-open issues that no maintainer has ever replied to.

    An issue the maintainers answered and left open on purpose — a tracked
    feature request, a known limitation — is not rot, however old it is. Only
    silence counts.
    """
    rotten = 0
    for issue in data.contribution_flow.oldest_open_issues:
        if (_days_since(issue.created_at) or 0) <= STALE_ISSUE_DAYS:
            continue
        author = (issue.last_comment_author or "").lower()
        if author and author in maintainers:
            continue
        rotten += 1
    return rotten


def _unfixed_advisory(data: RepoData) -> bool:
    """A direct dependency carrying a long-public advisory that has a fix."""
    advisories = data.dependencies.advisories
    if not advisories.collected:
        return False
    return any(
        finding.direct
        and finding.fixed_version
        and (finding.oldest_advisory_days or 0) > UNFIXED_ADVISORY_DAYS
        for finding in advisories.findings
    )


def _release_stall(data: RepoData) -> bool:
    """A release gap far past the project's own established cadence."""
    activity = data.activity
    gap = activity.days_since_latest_release
    mean = activity.mean_days_between_releases
    if gap is None or not mean:
        return False
    return gap > max(RELEASE_STALL_MULTIPLE * mean, RELEASE_STALL_FLOOR_DAYS)


def _scorecard_unmaintained(data: RepoData) -> bool:
    scorecard = data.security_signals.scorecard
    if scorecard is None:
        return False
    for check in scorecard.checks:
        if check.name == "Maintained":
            return check.score == 0
    return False


def _sole_maintainer_gone(data: RepoData) -> bool:
    """A one-person project whose one person is absent from the commit window."""
    maintainership = data.maintainership
    if maintainership.bus_factor != 1 or not maintainership.top_contributors:
        return False
    top = maintainership.top_contributors[0].login
    if not top:
        return False
    recent = {
        (c.author_login or "").lower()
        for c in data.activity.recent_commits
        if not c.is_bot
    }
    return top.lower() not in recent


def _broken_ci(data: RepoData) -> bool:
    flow = data.contribution_flow
    if flow.ci_last_conclusion is None:
        return False
    if flow.ci_last_conclusion.upper() in ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE"):
        return True
    return (_days_since(flow.ci_last_run_at) or 0) > DROUGHT_HARD_DAYS


def _guards(data: RepoData, maintainers: set[str]) -> list[Guard]:
    """Readings that explain the silence without abandonment."""
    guards: list[Guard] = []
    flow = data.contribution_flow

    replies = [
        days
        for item in (*flow.oldest_open_prs, *flow.oldest_open_issues)
        if (item.last_comment_author or "").lower() in maintainers
        and (days := _days_since(item.last_comment_at)) is not None
    ]
    if replies and min(replies) <= MAINTAINER_REPLY_DAYS:
        guards.append("maintainer_replying")

    open_issues = data.maintainership.issues.open_issues
    if not flow.oldest_open_prs and open_issues is not None and open_issues <= NO_DEMAND_MAX_OPEN_ISSUES:
        guards.append("no_open_demand")

    gap = data.activity.days_since_latest_release
    if gap is not None and gap <= DROUGHT_HARD_DAYS:
        guards.append("recent_release")

    advisories = data.dependencies.advisories
    if advisories.collected and advisories.affected_count == 0:
        guards.append("dependencies_clean")

    return guards


def _unverified(reason: UnverifiedReason, **extra) -> AbandonmentAssessment:
    return AbandonmentAssessment(
        state="unverified", factor=100, unverified_reason=reason, **extra
    )


def assess(data: RepoData) -> AbandonmentAssessment:
    """Assess whether the repository has been left unmaintained."""
    declared = _declared(data)
    if declared is not None:
        return AbandonmentAssessment(
            state="declared",
            factor=STATE_FACTOR["declared"],
            cap=STATE_CAP["declared"],
            declared_reason=declared,
        )

    age = _days_since(data.repo.created_at)
    if age is not None and age < MIN_AGE_DAYS:
        return _unverified("repository_too_young")

    drought_days, is_floor = _drought(data)
    if drought_days is None:
        return _unverified("no_commit_sample")

    if drought_days < DROUGHT_SOFT_DAYS:
        # A floor below the soft bar means the sampled window was all
        # automation and too short to see past: the true drought may be far
        # older. Reported as maintained regardless — understating a drought is
        # the safe direction, and the floor is recorded so the reader can see
        # the figure is a lower bound rather than an observation.
        return AbandonmentAssessment(
            state="maintained",
            factor=100,
            drought_days=drought_days,
            drought_is_floor=is_floor,
        )

    # Three of the seven obligation signals read the tracker queues. Without
    # them the evidence base is too thin to say more than "quiet", so an
    # unauthenticated or REST-fallback scan stops here rather than concluding
    # from the remainder.
    if not data.contribution_flow.collected:
        return _unverified("queues_not_read", drought_days=drought_days, drought_is_floor=is_floor)

    maintainers = _maintainer_logins(data)
    unanswered = _unanswered_prs(data)
    rotten = _rotten_issues(data, maintainers)
    last_merge = _days_since(data.contribution_flow.last_merged_pr_at)

    checks: list[tuple[Signal, bool]] = [
        (
            "unanswered_contributions",
            unanswered >= UNANSWERED_PRS_MIN
            and (last_merge is None or last_merge > DROUGHT_HARD_DAYS),
        ),
        ("issue_rot", rotten >= ROTTEN_ISSUES_MIN),
        ("unfixed_advisory", _unfixed_advisory(data)),
        ("release_stall", _release_stall(data)),
        ("scorecard_unmaintained", _scorecard_unmaintained(data)),
        ("sole_maintainer_gone", _sole_maintainer_gone(data)),
        ("broken_ci", _broken_ci(data)),
    ]
    signals = [key for key, hit in checks if hit]
    guards = _guards(data, maintainers)

    hard = drought_days >= DROUGHT_HARD_DAYS
    if len(guards) >= SUPPRESSING_GUARDS:
        state: State = "dormant"
    elif hard and len(signals) >= ABANDONED_SIGNALS:
        state = "likely_abandoned"
    elif len(signals) >= ABANDONED_SIGNALS or (hard and len(signals) >= AT_RISK_SIGNALS):
        state = "at_risk"
    else:
        state = "dormant"

    return AbandonmentAssessment(
        state=state,
        factor=STATE_FACTOR[state],
        cap=STATE_CAP[state],
        signals=signals,
        guards=guards,
        drought_days=drought_days,
        drought_is_floor=is_floor,
        unanswered_prs=unanswered,
        rotten_issues=rotten,
        days_since_last_merged_pr=last_merge,
    )
