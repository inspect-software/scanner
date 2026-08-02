"""Metric computation: turn raw RepoData into standardized 1..100 scores.

Design rules (see docs/metrics.md for the full methodology):

- Every metric is an integer in 1..100 — higher is better.
- Values map to standardized bands: critical / at_risk / moderate / good /
  excellent (thresholds in ``BAND_THRESHOLDS``).
- Each metric is a weighted sum of named components. Every component is
  reported with its earned/max points and a status: met, partial, missed, or
  excluded. Excluded components (no data, or not applicable) are removed and
  the remaining weights renormalized — missing data is never counted as zero.
- Metrics are grouped into weighted **categories**; each category has a
  rolled-up score (weighted mean of its available metrics). The overall score
  is the weighted mean of the available categories.
- If every component of a metric is excluded, the metric is None; a category
  with no scorable metric is dropped.
- Formulas are deterministic and versioned via ``METRICS_VERSION``.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Optional

from .models import (
    Activity,
    CommitRecord,
    Band,
    Metric,
    MetricCategory,
    MetricComponent,
    MetricNote,
    Metrics,
    OrgData,
    OrgMetrics,
    RepoData,
    ScanConfig,
    Scorecard,
)
from .license import license_for_report
from .abandonment import assess as abandonment_assess
from .growth import GrowthAssessment, assess as growth_assess
from .bots import is_dependency_bot
from .jurisdiction import assess_repo
from .scorecard import check_weight
from .vulns import SEVERITY_ORDER, STALE_ADVISORY_DAYS, penalty_units

METRICS_VERSION = "1.14.0"

# Credit awarded for each resolved license state (see license.py).
#
# A custom license is a real licence and scores most of the weight, but not
# all of it: a licence a machine cannot identify is a genuine adoption
# obstacle — downstream policy tooling, corporate review and package
# registries all key off recognized identifiers, and a reader cannot tell
# what they are permitted to do without reading the text themselves.
#
# This tier is deliberate. Before 1.3.0 custom licences already scored lower,
# but only because OpenSSF Scorecard happened to grade them 9/10 rather than
# 10/10 — a gap we inherited rather than chose, and never documented.
LICENSE_STATE_CREDIT: dict[str, float] = {
    "standard": 1.0,
    "custom": 0.75,
    "absent": 0.0,
}

# A source file above this size (bytes, ~1,500 lines) strains an agent's
# working context; used by the AI code-legibility metric.
AI_OVERSIZED_SOURCE_BYTES = 60_000
# An agent-instruction file smaller than this is treated as a stub, not
# substantive guidance (anti-gaming for the AI agent-context metric).
AI_AGENT_STUB_BYTES = 200
# Languages whose type systems are checkable out of the box (AI legibility).
STATICALLY_TYPED_LANGUAGES = {
    "TypeScript", "Go", "Rust", "Java", "Kotlin", "C#", "Scala", "Swift",
    "C++", "C", "Haskell", "OCaml", "F#", "Elm", "Dart",
}

# Detail string marking a component excluded because the scan configuration
# switched it off (as opposed to missing data). Used to phrase metric notes.
DISABLED_DETAIL = "disabled in scan configuration"
SCORECARD_UNAVAILABLE_DETAIL = "OpenSSF Scorecard unavailable"

# Lower bound of each band, checked from the top down.
BAND_THRESHOLDS: list[tuple[int, Band]] = [
    (85, "excellent"),
    (70, "good"),
    (50, "moderate"),
    (30, "at_risk"),
    (1, "critical"),
]


def band_for(value: int) -> Band:
    for threshold, band in BAND_THRESHOLDS:
        if value >= threshold:
            return band
    return "critical"


def _log_points(
    value: int,
    max_points: float,
    saturating_value: float,
    threshold: int = 0,
) -> float:
    """Log-scaled points: 0 at or below ``threshold``, ``max_points`` at ``saturating_value``.

    ``threshold`` is the highest value that still earns nothing; scoring ramps up
    from the next value. With the default of 0, any positive value earns points.
    """
    if value <= threshold:
        return 0.0
    scale = max_points / math.log10(saturating_value - threshold + 1)
    return min(max_points, math.log10(value - threshold + 1) * scale)


def _slug(name: str) -> str:
    """Stable key for a component, derived from its English name.

    Component names are prose and are shown translated; the key is what a
    localized surface looks the translation up by, and what stays constant if
    the English wording is ever reworded.
    """
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")


def _note(code: str, **params: Any) -> MetricNote:
    """One statement of a metric's note, identified rather than only written.

    Notes are generated English sentences, and generated text cannot be
    translated downstream. Every note a report writes is therefore also
    reported as a code and its values; ``Metric.note`` keeps the prose.
    """
    return MetricNote(code=code, params=params)


# A component's ``detail`` is generated English — the same problem as a metric
# note, and the same solution. The phrase is written from a template here and
# reported alongside its code and values, so a localized surface can state it
# in the reader's language rather than translate prose the scanner wrote.
#
# Text that came from *outside* the scanner stays a plain string and carries no
# code: an OpenSSF Scorecard reason, a registry's deprecation note, a homepage
# URL. None of it is ours to translate, and guessing at it would be worse than
# leaving it as its author published it.
DETAIL_TEMPLATES: dict[str, str] = {
    "no_data": "no data",
    # A bare list of file names the scan found, comma-separated. The names are
    # the repository's own; the code exists so the fragment has a slot at all.
    "file_list": "{files}",
    # Development activity
    "push_recency": "last push {days} days ago",
    "commit_cadence_weeks": "{weeks}/52 weeks with commits",
    "commits_last_year": "{count} commits in the last year",
    # AI Readiness — machine-authored maintenance
    "dependency_bot_commits": (
        "{count} of the last {sampled} commits are automated dependency updates"
    ),
    "dependency_bot_config_only": (
        "dependency automation configured, none observed in the sampled commits"
    ),
    "no_dependency_automation": "no automated dependency updates observed",
    "discounted_for_automation": (
        ", discounted for automation: {humans} of the last {sampled} "
        "commits human-authored, none in {span_days} days"
    ),
    # Release discipline
    "no_releases_published": "no releases published",
    "no_releases": "no releases",
    "version_tags_no_releases": "{count} version tags (no GitHub releases)",
    "releases_published": "{count} releases published",
    "release_recency": "latest release {days} days ago",
    "release_cadence": "a release every ~{gap:g} days",
    "release_cadence_unknown": "cadence unknown (single release)",
    # Popularity & adoption
    "stars": "{count:,} stars",
    "forks": "{count:,} forks",
    "watchers": "{count:,} watchers",
    "discounted_for_inorganic_growth": (
        ", discounted for inorganic growth: {stars:,} stars over "
        "{days} day(s) from {window}, {multiple:g}× the "
        "repository's own daily baseline"
    ),
    "discounted_for_inorganic_growth_plain": ", discounted for inorganic growth",
    # Maintainer resilience
    "bus_factor": "{count} contributor(s) cover half of all commits",
    "top_contributor_share": "top contributor authored {share}% of commits",
    "contributors_sampled": "{count} contributors",
    # Issue & PR responsiveness
    "issues_closed_share": "{share}% of issues closed",
    "no_issues_or_data": "no issues or no data",
    "no_decided_prs_or_data": "no decided pull requests or no data",
    "decided_prs_merged": "{merged}/{decided} decided PRs merged",
    # Ownership & stewardship
    "owner_organization": "organization-owned",
    "owner_personal": "personal (user) account",
    "not_applicable_to_user_accounts": "not applicable to user accounts",
    "owner_followers": "{count:,} followers of {login}",
    "public_repos": "{count} public repos",
    "account_age_years": ", account ~{years} yr old",
    # Community health (license)
    "license_standard": "recognized license",
    "license_custom": "license file present, not a recognized license",
    "license_absent": "no license file detected",
    "license_spdx": " ({spdx})",
    # Engineering practices
    "ci_workflows": "{count} workflow(s)",
    # Documentation
    "topics_count": "{count} topics",
    # Ecosystem adoption
    "downloads_monthly": "{count:,} downloads/month across {ecosystems}",
    "downloads_total": "{count:,} downloads all-time across {ecosystems}",
    "downloads_unavailable": "download stats unavailable",
    "registry_dependents": "{count:,} packages depend on it",
    "not_reported_by_this_ecosystem": "not reported by this ecosystem",
    # Package maintenance
    "packages_published": "{count} package(s) on {ecosystems}",
    "publish_recency": "latest publish {days} days ago",
    "published_versions": "{count} published versions",
    "package_deprecated": "{name}: deprecated/yanked on the registry",
    "package_not_deprecated": "active, not deprecated or yanked",
    # Security posture (OpenSSF Scorecard and file signals)
    "scorecard_check_not_reported": "not reported by this Scorecard version",
    "no_dependency_manifests": "no dependency manifests — not applicable",
    "lockfiles_not_expected": (
        "published library — lockfiles are an application concern, not expected"
    ),
    # Dependency advisories
    "no_direct_advisories": "no direct dependency carries a known advisory",
    "no_indirect_advisories": "no indirect dependency carries a known advisory",
    "advisories_affected": "{count} affected: {packages}",
    "advisories_affected_more": ", +{count} more",
    "advisories_scope_not_separable": (
        "transitive set not separable from development and test dependencies "
        "in this scope"
    ),
    "advisories_no_publication_date": "no advisory carries a publication date",
    "advisories_none_stale": "no advisory has been public longer than {days} days",
    "advisories_stale": (
        "{count} advisory-carrying package(s) unaddressed past "
        "{days} days; oldest published {oldest} days ago"
    ),
    # Malicious dependencies
    "no_malicious_dependencies": "no dependency is reported as a malicious package",
    "malicious_dependencies": "{count} reported as malicious: {packages}",
    "malicious_dependencies_more": ", +{count} more",
    "malicious_dependencies_withdrawn": (
        "{count} reported as malicious, but the registry no longer serves the resolved "
        "version — nothing installable remains"
    ),
    "malicious_dependencies_withdrawn_extra": (
        "; {count} more reported but no longer served by the registry"
    ),
    # Abandonment
    "abandonment_maintained": "last human commit {days} days ago",
    "abandonment_unverified": "maintenance record not established from the collected data",
    "abandonment_archived": "the repository is archived on GitHub",
    "abandonment_packages_deprecated": "every published package is deprecated or yanked",
    "abandonment_quiet": "no human commit for {days} days, with nothing left unanswered",
    "abandonment_flagged": "no human commit for {days} days; {count} unmet obligation(s): {signals}",
    "abandonment_drought_floor": " (at least, over the sampled commit window)",
    "abandonment_guarded": "; held at dormant by {guards}",
    # High-risk jurisdiction exposure
    "jurisdiction_no_match": "no confirmed policy-scope location match",
    "jurisdiction_exposure": "{country}: {role} ({count})",
    "jurisdiction_exposure_next": "; {country}: {role} ({count})",
    "jurisdiction_below_threshold": (
        "; {count} match(es) below the commit-weight threshold "
        "(<{commits} commits and <{share}% of commits), review-only"
    ),
    # AI readiness
    "no_agent_instructions": "no CLAUDE.md / AGENTS.md / editor rules",
    "agent_instructions_stub": " (stub)",
    "llms_txt_present": "llms.txt present",
    "legible_history": (
        "{legible} of {sampled} human commits state their intent "
        "(structured subject or explanatory body)"
    ),
    "toolchain_convention": "{files} (toolchain convention, no task runner)",
    "statically_typed_language": "{language} (statically typed)",
    "typecheck_config_language": "{language} with type-check config ({files})",
    "no_typecheck_config_language": "{language} without a type-check config",
    "primary_language_unknown": "primary language unknown",
    "oversized_source_files": "{oversized}/{sampled} source files over {kb}KB",
    "no_source_files": "no source files detected",
    "agent_authored_commits": (
        "{count} of the last {sampled} commits agent-authored or agent-credited"
    ),
    "no_agent_authored_commits": "no agent-authored commits among the last {sampled}",
    # Organization metrics
    "org_repos_pushed_90d": "{count}/{sampled} sampled repos pushed in the last 90 days",
    "org_repos_pushed_year": "{count}/{sampled} sampled repos pushed in the last year",
    "org_public_repositories": "{count} public repositories",
    "org_original_repos": "{count}/{sampled} sampled repos are not forks",
    "org_followers": "{count} followers",
    "org_stars_across_repos": "{count} stars across {sampled} sampled repos",
}

# Coded form of ``LICENSE_STATE_DETAIL`` — same three states, same wording.
LICENSE_STATE_DETAIL_CODE: dict[str, str] = {
    "standard": "license_standard",
    "custom": "license_custom",
    "absent": "license_absent",
}


def _d(code: str, **params: Any) -> MetricNote:
    """One coded fragment of a component's detail."""
    if code not in DETAIL_TEMPLATES:  # pragma: no cover - guards a typo at import time
        raise KeyError(f"unknown detail template: {code}")
    return MetricNote(code=code, params=params)


def _files(names: list[str]) -> Optional[MetricNote]:
    """The comma-separated file list a detail states, or None when empty."""
    if not names:
        return None
    return _d("file_list", files=", ".join(names))


def _pct(ratio: float) -> int:
    """The whole percent a ratio states, as a number.

    Templates that state a share write a literal ``%`` and take the rounded
    whole number, rather than carrying a ``:.0%`` format spec over a raw ratio:
    a localized surface formats numbers itself and cannot know which of its
    parameters are secretly ratios. Rounding goes through the same format spec
    the phrasing used to use, so the rendered English is unchanged.
    """
    return int(format(ratio, ".0%")[:-1])


def _render_detail(items: list[MetricNote]) -> str:
    """The English sentence the coded fragments spell out, concatenated."""
    return "".join(DETAIL_TEMPLATES[i.code].format(**i.params) for i in items)


def _comp(
    name: str,
    max_points: float,
    earned: Optional[float],
    detail: Optional[str | MetricNote | list[MetricNote]] = None,
) -> MetricComponent:
    """Build a component; ``earned=None`` marks it excluded (no data / N.A.)."""
    if isinstance(detail, MetricNote):
        detail = [detail]
    if isinstance(detail, list):
        details, detail = detail, _render_detail(detail)
    else:
        details = []
    if earned is None:
        return MetricComponent(
            key=_slug(name),
            name=name,
            points=0.0,
            max_points=max_points,
            status="excluded",
            detail=detail or "no data",
            details=details or [_d("no_data")],
        )
    earned = max(0.0, min(earned, max_points))
    if earned >= max_points - 1e-9:
        status = "met"
    elif earned <= 1e-9:
        status = "missed"
    else:
        status = "partial"
    return MetricComponent(
        key=_slug(name),
        name=name,
        points=round(earned, 1),
        max_points=max_points,
        status=status,
        detail=detail,
        details=details,
    )


def _check(
    name: str,
    condition: bool,
    weight: float,
    detail: Optional[str | MetricNote | list[MetricNote]] = None,
) -> MetricComponent:
    """Boolean checklist component: full points or none."""
    return _comp(name, weight, weight if condition else 0.0, detail)


def _scorecard_evidence(data: RepoData, check_name: str, weight: float) -> MetricComponent:
    """Return one cross-category OpenSSF Scorecard component.

    Scorecard remains the complete, risk-weighted Security metric.  A small
    number of checks also evidence a different health dimension (for example,
    CI-Tests evidences engineering practice).  Those cards use a deliberately
    small, category-specific weight -- never Scorecard's security risk weight.
    Missing or inconclusive Scorecard evidence is excluded, like every other
    unavailable input, rather than treated as a failure.
    """
    label = f"OpenSSF Scorecard: {check_name}"
    scorecard = data.security_signals.scorecard
    if scorecard is None:
        return _comp(label, weight, None, SCORECARD_UNAVAILABLE_DETAIL)
    check = next((item for item in scorecard.checks if item.name == check_name), None)
    if check is None:
        return _comp(label, weight, None, _d("scorecard_check_not_reported"))
    if check.score is None:
        return _comp(label, weight, None, check.reason or "inconclusive")
    return _comp(label, weight, check.score / 10.0 * weight, check.reason)


def _license_component(data: RepoData, weight: float) -> MetricComponent:
    """Single license signal for community health, from the resolved state.

    Since 1.3.0 this scores ``license.py``'s three-state resolution rather than
    OpenSSF Scorecard's grade directly. Two reasons. First, the tier is now
    ours and documented (``LICENSE_STATE_CREDIT``) instead of inherited from
    whatever Scorecard happens to award. Second, the resolver folds in *every*
    source — Scorecard included — so the score and the licence shown on the
    page can no longer disagree, which they did for the ~1% of repositories
    whose sources conflict.

    Resolving from the report's own fields means reports written before schema
    0.13.0 score identically to new ones without being rescanned.
    """
    info = license_for_report(data)
    credit = LICENSE_STATE_CREDIT[info.state]
    detail = [_d(LICENSE_STATE_DETAIL_CODE[info.state])]
    if info.state == "standard" and info.spdx_id:
        detail.append(_d("license_spdx", spdx=info.spdx_id))
    return _comp("License", weight, credit * weight, detail)


def _metric(
    key: str,
    name: str,
    components: list[MetricComponent],
    inputs: dict[str, Any],
) -> Optional[Metric]:
    scored = [c for c in components if c.status != "excluded"]
    possible = sum(c.max_points for c in scored)
    if possible <= 0:
        return None
    earned = sum(c.points for c in scored)
    value = max(1, min(100, round(100 * earned / possible)))
    excluded = [c for c in components if c.status == "excluded"]
    disabled = [c.name for c in excluded if c.detail == DISABLED_DETAIL]
    no_data = [
        c.name for c in excluded
        if c.detail not in (DISABLED_DETAIL, SCORECARD_UNAVAILABLE_DETAIL)
    ]
    note_parts: list[str] = []
    notes: list[MetricNote] = []
    if no_data:
        note_parts.append(f"Excluded from scoring (no data or not applicable): {', '.join(no_data)}.")
        notes.append(_note("excluded_no_data", components=_keys_of(excluded, no_data)))
    if disabled:
        note_parts.append(f"Disabled in scan configuration: {', '.join(disabled)}.")
        notes.append(_note("disabled_in_config", components=_keys_of(excluded, disabled)))
    if any(c.detail != SCORECARD_UNAVAILABLE_DETAIL for c in excluded):
        note_parts.append("Remaining weights renormalized.")
        notes.append(_note("weights_renormalized"))
    return Metric(
        key=key,
        name=name,
        value=value,
        band=band_for(value),
        components=components,
        notes=notes,
        inputs=inputs,
        note=" ".join(note_parts) if note_parts else None,
    )


def _keys_of(components: list[MetricComponent], names: list[str]) -> list[str]:
    """Component keys for the given names, in the order the names were given."""
    by_name = {c.name: c.key for c in components}
    return [by_name.get(n, _slug(n)) for n in names]


def _disable_components(metric: Metric, disabled_names: set[str]) -> Optional[Metric]:
    """Re-score a metric with the named components switched off by configuration.

    Each named component becomes ``excluded`` and its weight is renormalized
    away, mirroring how missing data is handled. Returns None if switching them
    off leaves the metric with nothing scorable."""
    if not disabled_names:
        return metric
    updated: list[MetricComponent] = []
    changed = False
    for c in metric.components:
        if c.name in disabled_names and c.status != "excluded":
            updated.append(
                MetricComponent(
                    key=c.key,
                    name=c.name,
                    points=0.0,
                    max_points=c.max_points,
                    status="excluded",
                    detail=DISABLED_DETAIL,
                )
            )
            changed = True
        else:
            updated.append(c)
    if not changed:
        return metric
    return _metric(metric.key, metric.name, updated, metric.inputs)


# ---------------------------------------------------------------------------
# Repository metrics
# ---------------------------------------------------------------------------


def metric_development_activity(data: RepoData) -> Optional[Metric]:
    """Is code actively being written? (push recency, cadence, volume)"""
    a = data.activity

    if a.days_since_last_push is not None:
        d = a.days_since_last_push
        pts = 40.0 if d <= 7 else 32.0 if d <= 30 else 20.0 if d <= 90 else 11.0 if d <= 180 else 4.0 if d <= 365 else 0.0
        recency = _comp("Push recency", 36, pts / 40 * 36, _d("push_recency", days=d))
    else:
        recency = _comp("Push recency", 36, None)

    human, human_note = _human_factor(a)

    if a.active_weeks_last_year is not None:
        w = min(a.active_weeks_last_year, 52)
        cadence = _comp(
            "Commit cadence", 36, 36.0 * w / 52 * human,
            [_d("commit_cadence_weeks", weeks=w), *human_note],
        )
    else:
        cadence = _comp("Commit cadence", 36, None)

    if a.commits_last_year is not None:
        n = a.commits_last_year
        volume = _comp(
            "Commit volume", 18, _log_points(n, 18, 100) * human,
            [_d("commits_last_year", count=n), *human_note],
        )
    else:
        volume = _comp("Commit volume", 18, None)

    return _metric(
        "development_activity",
        "Development activity",
        [recency, cadence, volume, _scorecard_evidence(data, "Maintained", 10)],
        {
            "days_since_last_push": a.days_since_last_push,
            "active_weeks_last_year": a.active_weeks_last_year,
            "commits_last_year": a.commits_last_year,
            "human_commit_share": _human_commit_share(a),
        },
    )


# Cadence and volume count a Dependabot bump as development: both read GitHub
# counters that cannot tell a maintainer's work from a robot's. The human share
# of the sampled commit window discounts them rather than scoring as a component
# of its own — an additive component cannot express this. Measured: adding one
# worth 15 points *raised* caolan/async by 4, because a half-earned component
# lifts any metric already scoring below half. A signal meant to catch inflation
# must deflate the inflated inputs, not sit beside them.
#
# The precedent is the high-risk jurisdiction multiplier: a documented factor
# over a component's value, carrying no additive weight of its own.
#
# Full marks at this share and above. It is a floor test for human involvement,
# not a preference for manual work: kubernetes runs 48% bot commits and is
# plainly maintained, so the threshold sits below that and no ordinary project
# pays anything. Only automation-sustained repositories lose points.
HUMAN_COMMIT_SHARE_FULL_MARKS = 0.40

# Below this many sampled commits the share is too noisy to act on — a young
# repository with four commits should not be judged on one bot bump.
HUMAN_COMMIT_SAMPLE_MIN = 20

# How long the machines must have been committing alone before a low human
# share counts against a project. Calibrated on the live catalogue: among the
# repositories where automation authors most of the traffic, the healthy ones
# had a human commit within 0-6 days (starship 2, pulumi-gcp 0, aqua-registry
# 0), while the automation-sustained ones stood at 104 and 125 days.
HUMAN_COMMIT_STALE_DAYS = 90


def _human_commit_share(activity: Activity) -> Optional[float]:
    """Share of the sampled commit window authored by people, or None."""
    commits = activity.recent_commits
    if len(commits) < HUMAN_COMMIT_SAMPLE_MIN:
        return None
    return round(sum(1 for c in commits if not c.is_bot) / len(commits), 3)


def _bot_only_days(commits: list[CommitRecord]) -> int:
    """How long the sample has run without a human commit.

    Measured inside the window — newest commit minus newest human commit —
    rather than against the clock, so the figure is reproducible from the
    stored report and does not drift between rescorings.

    When the window contains no human commit at all the true gap predates it
    and is unknowable; the window's own span is returned as the lower bound.
    """
    newest = commits[0].committed_at
    for commit in commits:  # newest-first
        if not commit.is_bot:
            return (newest - commit.committed_at).days
    return (newest - commits[-1].committed_at).days


def _human_factor(activity: Activity) -> tuple[float, list[MetricNote]]:
    """Discount factor for the commit-derived components, and its evidence.

    The evidence is returned as detail fragments appended to whichever
    components the factor discounted.

    Two conditions must hold together, and the second exists because the first
    alone was wrong. A low human share on its own says only that a project
    automates heavily, which the healthiest projects do: measured on the live
    catalogue, starship runs 76% bot commits with a human commit two days old,
    aquaproj/aqua-registry 93% — automated version bumps *are* its product —
    and pulumi-gcp 81% as a generated SDK. Discounting those was a false
    penalty on three of the best-maintained repositories in the sample.

    What separates them from a genuinely automation-sustained project is not
    the share but the silence behind it: async's newest human commit was some
    two years old, material-symbols' 125 days. So the discount applies only
    when the machines have also been running alone for a while.

    Returns (1.0, []) whenever the sample is missing, too small, or either
    condition fails — an unauthenticated scan or a REST fallback must never
    cost a repository points for data the scan did not collect.
    """
    share = _human_commit_share(activity)
    if share is None or share >= HUMAN_COMMIT_SHARE_FULL_MARKS:
        return 1.0, []

    commits = activity.recent_commits
    bot_only_days = _bot_only_days(commits)
    if bot_only_days <= HUMAN_COMMIT_STALE_DAYS:
        return 1.0, []

    humans = sum(1 for c in commits if not c.is_bot)
    note = _d(
        "discounted_for_automation",
        humans=humans,
        sampled=len(commits),
        span_days=bot_only_days,
    )
    return share / HUMAN_COMMIT_SHARE_FULL_MARKS, [note]


SIGNAL_LABELS: dict[str, str] = {
    "unanswered_contributions": "pull requests unanswered",
    "issue_rot": "issues unanswered",
    "unfixed_advisory": "a fixed advisory left unapplied",
    "release_stall": "releases stalled",
    "scorecard_unmaintained": "Scorecard reports it unmaintained",
    "sole_maintainer_gone": "its sole maintainer absent",
    "broken_ci": "CI broken",
}
GUARD_LABELS: dict[str, str] = {
    "maintainer_replying": "a maintainer still replying",
    "no_open_demand": "nothing open to answer",
    "recent_release": "a release within the year",
    "dependencies_clean": "no affected dependency",
}


def metric_abandonment(data: RepoData) -> Optional[Metric]:
    """Has the project been left unmaintained? (red flag, no additive weight)

    Silence is not the finding — see ``abandonment.py`` for why, and for the
    evidence tiers. This metric only carries the assessment into the report:
    its value is the multiplier applied to the weighted overall score, exactly
    like the two Security red flags, and it earns a repository nothing when
    clean.

    Never ``None``: an assessment that could reach no conclusion is reported as
    ``unverified`` at a 100% multiplier, so the reader can see that the
    question was asked and left open rather than silently skipped.
    """
    result = abandonment_assess(data)
    days = result.drought_days

    if result.state == "declared":
        detail = [_d(f"abandonment_{result.declared_reason}")]
    elif result.state == "unverified":
        detail = [_d("abandonment_unverified")]
    elif result.state == "maintained":
        detail = [_d("abandonment_maintained", days=days)]
    elif result.flagged:
        detail = [
            _d(
                "abandonment_flagged",
                days=days,
                count=len(result.signals),
                signals=", ".join(SIGNAL_LABELS[s] for s in result.signals),
            )
        ]
        if result.drought_is_floor:
            detail.append(_d("abandonment_drought_floor"))
    else:
        detail = [_d("abandonment_quiet", days=days)]
        if result.drought_is_floor:
            detail.append(_d("abandonment_drought_floor"))
        if result.guards:
            detail.append(
                _d("abandonment_guarded", guards=", ".join(GUARD_LABELS[g] for g in result.guards))
            )

    return _metric(
        "abandonment",
        "Abandonment",
        [_comp("Project is still maintained", 100, result.factor, detail)],
        {
            "red_flag": result.flagged,
            "state": result.state,
            "multiplier_pct": result.factor,
            "cap": result.cap,
            "signals": result.signals,
            "guards": result.guards,
            "declared_reason": result.declared_reason,
            "unverified_reason": result.unverified_reason,
            "days_since_last_human_commit": days,
            "days_since_last_human_commit_is_floor": result.drought_is_floor,
            "unanswered_open_prs": result.unanswered_prs,
            "unanswered_open_issues": result.rotten_issues,
            "days_since_last_merged_pr": result.days_since_last_merged_pr,
        },
    )


def metric_release_discipline(data: RepoData) -> Optional[Metric]:
    """Does the project ship versioned releases on a healthy cadence?"""
    a = data.activity
    if a.releases_count is None:
        return None

    if not a.releases_count:
        ships = _comp("Ships releases", 27, 0.0, _d("no_releases_published"))
        recency = _comp("Release recency", 36, 0.0, _d("no_releases"))
        cadence = _comp("Release cadence", 27, 0.0, _d("no_releases"))
        return _metric(
            "release_discipline", "Release discipline", [
                ships, recency, cadence, _scorecard_evidence(data, "Signed-Releases", 10),
            ],
            {"releases_count": 0},
        )

    if a.releases_from_tags:
        # Versioned tags without GitHub Releases: real cadence, but the project
        # forgoes the Releases workflow (changelogs, assets, signing), so this
        # earns partial credit rather than the full "ships releases" points.
        ships = _comp(
            "Ships releases", 27, 16.2,
            _d("version_tags_no_releases", count=a.releases_count),
        )
    else:
        ships = _comp(
            "Ships releases", 27, 27.0, _d("releases_published", count=a.releases_count)
        )

    if a.days_since_latest_release is not None:
        d = a.days_since_latest_release
        pts = 36.0 if d <= 90 else 27.0 if d <= 180 else 16.2 if d <= 365 else 7.2 if d <= 730 else 0.0
        recency = _comp("Release recency", 36, pts, _d("release_recency", days=d))
    else:
        recency = _comp("Release recency", 36, None)

    if a.mean_days_between_releases is not None:
        gap = a.mean_days_between_releases
        pts = 27.0 if gap <= 45 else 19.8 if gap <= 120 else 12.6 if gap <= 365 else 5.4
        cadence = _comp("Release cadence", 27, pts, _d("release_cadence", gap=gap))
    else:
        cadence = _comp("Release cadence", 27, 12.6, _d("release_cadence_unknown"))

    return _metric(
        "release_discipline",
        "Release discipline",
        [ships, recency, cadence, _scorecard_evidence(data, "Signed-Releases", 10)],
        {
            "releases_count": a.releases_count,
            "releases_from_tags": a.releases_from_tags,
            "days_since_latest_release": a.days_since_latest_release,
            "mean_days_between_releases": a.mean_days_between_releases,
            "latest_release_tag": a.latest_release_tag,
        },
    )


# Stars and forks are the only inputs in the model that can be bought outright,
# in bulk, from a public marketplace. Where the collected day-by-day history
# shows a burst that organic attention does not produce, the two purchasable
# components are discounted — the same shape as the human-authorship factor
# above, and the jurisdiction multiplier before it: the inflated input is
# deflated rather than scored beside a new component. Watchers are left alone;
# they are not part of what the anomaly evidences.
#
# See growth.py for the detection rules and for why a burst on its own is
# never a finding.
def _growth_factor(data: RepoData) -> tuple[float, list[MetricNote], GrowthAssessment]:
    """Discount factor for the purchasable components, and its evidence.

    The evidence is returned as detail fragments appended to the components the
    factor discounted."""
    assessment = growth_assess(data)
    if not assessment.flagged:
        return 1.0, [], assessment
    window = assessment.peak_window
    note = (
        _d(
            "discounted_for_inorganic_growth",
            stars=window.stars,
            days=window.days,
            window=window.label(),
            multiple=window.multiple,
        )
        if window is not None
        else _d("discounted_for_inorganic_growth_plain")
    )
    return assessment.factor, [note], assessment


def metric_popularity(data: RepoData) -> Optional[Metric]:
    """How much adoption and attention does the project have?"""
    p = data.popularity
    growth, growth_note, assessment = _growth_factor(data)
    stars = _comp(
        "Stars", 60, _log_points(p.stars, 60, 5000, threshold=2) * growth,
        [_d("stars", count=p.stars), *growth_note],
    )
    forks = _comp(
        "Forks", 25, _log_points(p.forks, 25, 1000, threshold=2) * growth,
        [_d("forks", count=p.forks), *growth_note],
    )
    watchers = _comp(
        "Watchers", 15, _log_points(p.watchers, 15, 500, threshold=2),
        _d("watchers", count=p.watchers),
    )
    inputs: dict[str, Any] = {
        "stars": p.stars,
        "forks": p.forks,
        "watchers": p.watchers,
        "growth_state": assessment.state,
        "growth_factor_pct": round(assessment.factor * 100),
    }
    if assessment.reason:
        inputs["growth_unverified_reason"] = assessment.reason
    if assessment.flagged:
        window = assessment.peak_window
        inputs["growth_signals"] = list(assessment.signals)
        # ISO 8601 intervals of every confirmed window, so a chart can mark the
        # days the finding is about instead of asking a reader to locate them
        # from a sentence.
        inputs["growth_windows"] = [
            f"{w.start.isoformat()}/{w.end.isoformat()}"
            for w in assessment.windows
            if w.confirmed
        ]
        if window is not None:
            inputs["growth_peak_window"] = window.label()
            inputs["growth_peak_stars"] = window.stars
            inputs["growth_peak_multiple"] = window.multiple
            inputs["growth_peak_days"] = window.days
        inputs["growth_baseline_per_day"] = assessment.baseline_per_day
        inputs["growth_top_days_share"] = assessment.top_days_share
        inputs["growth_history_complete"] = assessment.history_complete
    metric = _metric("popularity", "Popularity & adoption", [stars, forks, watchers], inputs)
    if metric is not None and assessment.flagged:
        discount = round((1 - assessment.factor) * 100)
        metric.note = (
            "Inorganic Growth Policy discounts the stars and forks components by "
            f"{discount}%. The finding describes the timing of "
            "public star and fork events; it does not establish that attention was "
            "purchased, or that the maintainers were involved."
        )
        metric.notes.append(_note("growth_policy_discount", pct=discount))
    return metric


def metric_maintainer_resilience(data: RepoData) -> Optional[Metric]:
    """Can the project survive losing its top maintainer? (bus factor)"""
    m = data.maintainership
    if m.bus_factor is None:
        return None

    bf = m.bus_factor
    bf_pts = {1: 9.0, 2: 25.2, 3: 36.0, 4: 43.2}.get(bf, min(54.0, 43.2 + (bf - 4) * 2.7))
    bus = _comp("Bus factor", 54, bf_pts, _d("bus_factor", count=bf))

    if m.top_contributor_share is not None:
        share = m.top_contributor_share
        distribution = _comp(
            "Commit distribution", 22.5, (1.0 - share) * 22.5,
            _d("top_contributor_share", share=_pct(share)),
        )
    else:
        distribution = _comp("Commit distribution", 22.5, None)

    if m.contributors_sampled is not None:
        breadth = _comp(
            "Contributor breadth", 13.5, min(13.5, m.contributors_sampled * 1.35),
            _d("contributors_sampled", count=m.contributors_sampled),
        )
    else:
        breadth = _comp("Contributor breadth", 13.5, None)

    return _metric(
        "maintainer_resilience",
        "Maintainer resilience (bus factor)",
        [bus, distribution, breadth, _scorecard_evidence(data, "Contributors", 10)],
        {
            "bus_factor": m.bus_factor,
            "top_contributor_share": m.top_contributor_share,
            "contributors_sampled": m.contributors_sampled,
        },
    )


def metric_responsiveness(data: RepoData) -> Optional[Metric]:
    """Are issues and pull requests actually being handled?"""
    issues = data.maintainership.issues

    if issues.closed_ratio is not None:
        issue_component = _comp(
            "Issue resolution", 46.75, issues.closed_ratio * 46.75,
            _d("issues_closed_share", share=_pct(issues.closed_ratio)),
        )
    else:
        issue_component = _comp("Issue resolution", 46.75, None, _d("no_issues_or_data"))

    pr_component = _comp("PR acceptance", 38.25, None, _d("no_decided_prs_or_data"))
    if issues.merged_prs is not None and issues.closed_unmerged_prs is not None:
        decided = issues.merged_prs + issues.closed_unmerged_prs
        if decided > 0:
            pr_component = _comp(
                "PR acceptance", 38.25, issues.merged_prs / decided * 38.25,
                _d("decided_prs_merged", merged=issues.merged_prs, decided=decided),
            )

    return _metric(
        "responsiveness",
        "Issue & PR responsiveness",
        [issue_component, pr_component, _scorecard_evidence(data, "Code-Review", 15)],
        {
            "open_issues": issues.open_issues,
            "closed_issues": issues.closed_issues,
            "issue_closed_ratio": issues.closed_ratio,
            "merged_prs": issues.merged_prs,
            "closed_unmerged_prs": issues.closed_unmerged_prs,
        },
    )


def metric_stewardship(data: RepoData) -> Optional[Metric]:
    """Who stands behind this repo? Organization backing, reach, track record.

    This is where being owned by an organization (vs. a personal account)
    influences the score: an organization — especially one with a verified
    domain and reach — signals shared, accountable stewardship that outlives
    any single person.
    """
    owner = data.owner
    if owner is None:
        return None

    is_org = owner.type == "Organization"
    backing = _comp(
        "Ownership backing", 30, 30.0 if is_org else 10.0,
        _d("owner_organization") if is_org else _d("owner_personal"),
    )

    # Verified-domain badge only exists for organizations; N/A for users.
    if is_org:
        verified = _check("Verified domain", bool(owner.is_verified), 20)
    else:
        verified = _comp(
            "Verified domain", 20, None, _d("not_applicable_to_user_accounts")
        )

    reach = _comp(
        "Owner reach", 25, _log_points(owner.followers, 25, 3000),
        _d("owner_followers", count=owner.followers, login=owner.login),
    )

    if owner.account_age_days is not None:
        age_years = owner.account_age_days / 365.25
        age_pts = min(12.0, age_years / 6 * 12.0)
    else:
        age_pts = None
    repos_pts = min(13.0, _log_points(owner.public_repos, 13, 60))
    track_pts = None if age_pts is None else age_pts + repos_pts
    track_detail = [_d("public_repos", count=owner.public_repos)]
    if owner.account_age_days:
        track_detail.append(_d("account_age_years", years=owner.account_age_days // 365))
    track = _comp("Track record", 25, track_pts, track_detail)

    return _metric(
        "stewardship",
        "Ownership & stewardship",
        [backing, verified, reach, track],
        {
            "owner_login": owner.login,
            "owner_type": owner.type,
            "is_verified": owner.is_verified,
            "followers": owner.followers,
            "public_repos": owner.public_repos,
            "account_age_days": owner.account_age_days,
        },
    )


def metric_community_health(data: RepoData) -> Optional[Metric]:
    """Is the project set up to receive users and contributors?"""
    c = data.community
    checklist = [
        _check("README", c.has_readme, 22.5),
        _license_component(data, 22.5),
        _check("CONTRIBUTING guide", c.has_contributing, 18),
        _check("Code of conduct", c.has_code_of_conduct, 13.5),
        _check("Issue template", c.has_issue_template, 7.2),
        _check("PR template", c.has_pull_request_template, 6.3),
    ]
    return _metric(
        "community_health",
        "Community health",
        checklist,
        {
            "has_readme": c.has_readme,
            "has_license": c.has_license,
            "has_contributing": c.has_contributing,
            "has_code_of_conduct": c.has_code_of_conduct,
            "has_issue_template": c.has_issue_template,
            "has_pull_request_template": c.has_pull_request_template,
        },
    )


def metric_engineering_practices(data: RepoData) -> Optional[Metric]:
    """Baseline engineering hygiene: CI, tests, linting."""
    q = data.quality_signals
    checklist = [
        _check(
            "CI workflows", q.has_ci, 24,
            _d("ci_workflows", count=len(q.ci_workflows)) if q.has_ci else None,
        ),
        _check("Tests present", q.has_tests, 24),
        _check(
            "Linter config", q.has_linter_config, 16,
            _files(q.linter_configs),
        ),
        _check("Pre-commit hooks", q.has_precommit_config, 9.6),
        _check(".editorconfig", q.has_editorconfig, 6.4),
        _scorecard_evidence(data, "CI-Tests", 20),
    ]
    return _metric(
        "engineering_practices",
        "Engineering practices",
        checklist,
        {
            "has_ci": q.has_ci,
            "has_tests": q.has_tests,
            "has_linter_config": q.has_linter_config,
            "has_precommit_config": q.has_precommit_config,
            "has_editorconfig": q.has_editorconfig,
        },
    )


def metric_documentation(data: RepoData) -> Optional[Metric]:
    """Can a newcomer find out what this is and how to use it?"""
    c = data.community
    q = data.quality_signals
    repo = data.repo
    checklist = [
        _check("README", c.has_readme, 30),
        _check("Documentation directory", q.has_docs_dir, 25),
        _check("Documentation / homepage site", bool(repo.homepage), 15, repo.homepage or None),
        _check("Repository description", c.has_description, 10),
        _check(
            "Topics", bool(repo.topics), 10,
            _d("topics_count", count=len(repo.topics)) if repo.topics else None,
        ),
        _check("Wiki", bool(repo.has_wiki), 10),
    ]
    return _metric(
        "documentation",
        "Documentation",
        checklist,
        {
            "has_readme": c.has_readme,
            "has_docs_dir": q.has_docs_dir,
            "homepage": repo.homepage,
            "has_description": c.has_description,
            "topics": repo.topics,
            "has_wiki": repo.has_wiki,
        },
    )


def _scored_packages(data: RepoData) -> list:
    """Published packages that belong to this repo (registry repo URL matches,
    or the registry declares none — benefit of the doubt since the manifest is
    in the repo)."""
    return [
        p for p in data.ecosystem.packages if p.exists and p.matches_repo is not False
    ]


def metric_ecosystem_adoption(data: RepoData) -> Optional[Metric]:
    """How widely is the published package actually installed?

    Real download counts from the package registry are a far stronger adoption
    signal than GitHub stars — people install libraries they never star.
    ``None`` for repos that publish nothing (or when no download data is
    available), so non-package repos are never penalized.
    """
    packages = _scored_packages(data)
    if not packages:
        return None

    monthly_values = [p.monthly_downloads for p in packages if p.monthly_downloads is not None]
    total_values = [p.total_downloads for p in packages if p.total_downloads is not None]
    dependents_values = [p.dependents_count for p in packages if p.dependents_count is not None]
    if not monthly_values and not total_values and not dependents_values:
        return None

    ecosystems = ", ".join(sorted({p.ecosystem for p in packages}))
    # Prefer monthly downloads; fall back to lifetime totals for registries that
    # publish no monthly figure (e.g. RubyGems), at a higher saturation point.
    if monthly_values:
        monthly = sum(monthly_values)
        downloads = _comp(
            "Monthly downloads", 80, _log_points(monthly, 80, 1_000_000),
            _d("downloads_monthly", count=monthly, ecosystems=ecosystems),
        )
    elif total_values:
        total = sum(total_values)
        downloads = _comp(
            "Total downloads", 80, _log_points(total, 80, 50_000_000),
            _d("downloads_total", count=total, ecosystems=ecosystems),
        )
    else:
        downloads = _comp("Downloads", 80, None, _d("downloads_unavailable"))

    if dependents_values:
        dependents = sum(dependents_values)
        dep = _comp(
            "Registry dependents", 20, _log_points(dependents, 20, 1000),
            _d("registry_dependents", count=dependents),
        )
    else:
        dep = _comp(
            "Registry dependents", 20, None, _d("not_reported_by_this_ecosystem")
        )

    return _metric(
        "ecosystem_adoption",
        "Ecosystem adoption (downloads)",
        [downloads, dep],
        {
            "ecosystems": ecosystems,
            "monthly_downloads": sum(monthly_values) if monthly_values else None,
            "total_downloads": sum(total_values) if total_values else None,
            "dependents": sum(dependents_values) if dependents_values else None,
            "packages": [p.name for p in packages],
        },
    )


def metric_package_maintenance(data: RepoData) -> Optional[Metric]:
    """Is the published package current, versioned, and not deprecated?

    Registry publish recency and deprecation/abandonment are distinct from
    GitHub activity — a library can go stale or be marked abandoned on the
    registry while its repo still sees commits, and vice versa.
    """
    packages = _scored_packages(data)
    if not packages:
        return None

    ecosystems = ", ".join(sorted({p.ecosystem for p in packages}))
    published = _comp(
        "Published & resolvable", 25, 25.0,
        _d("packages_published", count=len(packages), ecosystems=ecosystems),
    )

    recency_days = [p.days_since_latest_publish for p in packages
                    if p.days_since_latest_publish is not None]
    if recency_days:
        d = min(recency_days)
        pts = 35.0 if d <= 180 else 26.0 if d <= 365 else 14.0 if d <= 730 else 4.0
        recency = _comp("Publish recency", 35, pts, _d("publish_recency", days=d))
    else:
        recency = _comp("Publish recency", 35, None)

    version_counts = [p.versions_count for p in packages if p.versions_count is not None]
    if version_counts:
        n = max(version_counts)
        pts = 20.0 if n >= 5 else 12.0 if n >= 2 else 4.0
        history = _comp("Version history", 20, pts, _d("published_versions", count=n))
    else:
        history = _comp("Version history", 20, None)

    deprecated = [p for p in packages if p.is_deprecated or p.latest_version_yanked]
    if deprecated:
        # The registry's own deprecation note is not ours to translate, so a
        # detail carrying one stays a plain string with no code; only the
        # fallback wording — which the scanner wrote — is coded.
        registry_note = deprecated[0].deprecation_note
        detail: str | MetricNote = (
            f"{deprecated[0].name}: {registry_note}"
            if registry_note
            else _d("package_deprecated", name=deprecated[0].name)
        )
        health = _comp("Not deprecated", 20, 0.0, detail)
    else:
        health = _comp("Not deprecated", 20, 20.0, _d("package_not_deprecated"))

    return _metric(
        "package_maintenance",
        "Package maintenance",
        [published, recency, history, health],
        {
            "ecosystems": ecosystems,
            "packages": [p.name for p in packages],
            "min_days_since_publish": min(recency_days) if recency_days else None,
            "any_deprecated": bool(deprecated),
        },
    )


def _security_from_scorecard(sc: Scorecard) -> Optional[Metric]:
    """Security posture from OpenSSF Scorecard checks.

    Each check becomes a component weighted by Scorecard's own risk level, so
    the rolled-up value tracks Scorecard's aggregate. A check Scorecard could
    not determine (``score is None``, i.e. it returned -1) is excluded and its
    weight renormalized away — never scored as zero. This is what makes the
    metric tool-agnostic: any accepted SAST / dependency-update / signing tool
    earns the relevant check, and undetectable practices don't tank the score.
    """
    components: list[MetricComponent] = []
    for check in sc.checks:
        weight = check_weight(check.name)
        if check.score is None:
            components.append(_comp(check.name, weight, None, check.reason or "inconclusive"))
        else:
            components.append(
                _comp(check.name, weight, check.score / 10.0 * weight, check.reason)
            )
    return _metric(
        "security_posture",
        "Security posture",
        components,
        {
            "source": "openssf_scorecard",
            "scorecard_aggregate": sc.aggregate_score,
            "scorecard_version": sc.scorecard_version,
            "checks_evaluated": sum(1 for c in sc.checks if c.score is not None),
            "checks_inconclusive": sum(1 for c in sc.checks if c.score is None),
        },
    )


def _security_from_files(data: RepoData) -> Optional[Metric]:
    """Fallback security posture from file-tree signals, used when OpenSSF
    Scorecard is unavailable. Coarser and more vendor-specific than Scorecard —
    kept only so a report still carries a security signal without the CLI."""
    s = data.security_signals

    # A committed lockfile pins dependencies — but that is an *application*
    # concern. A published library/gem is expected NOT to commit one (Bundler
    # tells gem authors not to check in Gemfile.lock; npm/PyPI libraries specify
    # ranges too), so scoring its absence would penalize every well-behaved
    # package. Only score lockfiles for repos that declare dependencies AND do
    # not publish a package; otherwise exclude and renormalize.
    if not data.dependencies.manifests:
        lockfiles = _comp(
            "Dependency lockfiles", 25, None, _d("no_dependency_manifests")
        )
    elif _scored_packages(data):
        lockfiles = _comp(
            "Dependency lockfiles", 25, None, _d("lockfiles_not_expected")
        )
    else:
        lockfiles = _check(
            "Dependency lockfiles", bool(s.lockfiles), 25, _files(s.lockfiles)
        )

    components = [
        _check("Security policy (SECURITY.md)", s.has_security_policy, 30),
        _check("Dependabot config", s.has_dependabot_config, 25),
        lockfiles,
        _check("CodeQL workflow", s.has_codeql_workflow, 20),
    ]
    return _metric(
        "security_posture",
        "Security posture",
        components,
        {
            "source": "file_signals",
            "has_security_policy": s.has_security_policy,
            "has_dependabot_config": s.has_dependabot_config,
            "has_codeql_workflow": s.has_codeql_workflow,
            "lockfiles": s.lockfiles,
            "manifests": data.dependencies.manifests,
        },
    )


def metric_security_posture(data: RepoData) -> Optional[Metric]:
    """Security posture — OpenSSF Scorecard when available, file checks otherwise.

    Scorecard is a neutral, tool-agnostic standard; we prefer it over detecting
    specific vendor config files. When the Scorecard CLI didn't run (not
    installed, failed, or disabled), we fall back to coarse file-tree signals.
    """
    sc = data.security_signals.scorecard
    if sc is not None:
        metric = _security_from_scorecard(sc)
        if metric is not None:
            return metric
    return _security_from_files(data)


# Penalty units of *additional* exposure, beyond the worst finding, that halve
# what remains of a component. The volume term is hyperbolic rather than
# exponential on purpose: the marginal harm of the Nth advisory falls away, but
# the score never reaches zero, so ordering survives at any volume. Exponential
# decay — the 1.5.0 formulation — bottomed out and made 30 findings
# indistinguishable from 300.
ADVISORY_VOLUME_SCALE = 4.0

# How much of a component the single worst finding can remove. At 0.8 one
# maximal-severity finding (1.0 units, CVSS 10) leaves 20% of the component
# standing, so volume still has room to discriminate below it.
ADVISORY_WORST_WEIGHT = 0.8


def _advisory_component(
    name: str, max_points: float, findings: list, scope: str
) -> MetricComponent:
    """One advisory component, scored as *ceiling × volume decay*.

    A pure decay over summed severity — the 1.5.0 formulation — saturated: past
    roughly five findings every result collapsed to zero and the metric stopped
    distinguishing a project with five advisories from one with three hundred.
    It also weighted a hundred low-severity findings the same as one critical.

    So the worst single finding sets a **ceiling** on the component, and
    remaining volume decays what is left of it. That matches how the finding
    is actually read — one critical is bad regardless of what else is there,
    and the fiftieth low-severity advisory changes little — and it keeps the
    component strictly positive, so ordering survives at any volume.

    Per-finding units come from the CVSS base score where the advisory
    publishes a vector, falling back to the coarse database label.
    """
    if not findings:
        return _comp(name, max_points, max_points, _d(f"no_{scope}_advisories"))

    units = [penalty_units(f.severity, f.cvss_score) for f in findings]
    worst = max(units)
    ceiling = 1.0 - ADVISORY_WORST_WEIGHT * worst
    decay = 1.0 / (1.0 + (sum(units) - worst) / ADVISORY_VOLUME_SCALE)
    earned = max_points * ceiling * decay

    top = ", ".join(
        f"{f.name} {f.version} ({f.severity}{f' {f.cvss_score:.1f}' if f.cvss_score else ''})"
        for f in findings[:3]
    )
    detail = [_d("advisories_affected", count=len(findings), packages=top)]
    if len(findings) > 3:
        detail.append(_d("advisories_affected_more", count=len(findings) - 3))
    return _comp(name, max_points, earned, detail)


def _stale_advisory_component(max_points: float, findings: list) -> MetricComponent:
    """Advisories whose fix has been public long enough to have been applied.

    This is the dimension the earlier formulation lacked entirely. Being hit by
    an advisory published last week is bad luck; still resolving to a version
    affected by an advisory published a year ago is a maintenance failure, and
    the two should not score alike. Advisory publication date is the proxy for
    fix availability — OSV carries it on every record.

    Excluded when no finding has a publication date, rather than assumed fresh.
    """
    dated = [f for f in findings if f.oldest_advisory_days is not None]
    if not dated:
        return _comp(
            "No advisories left outstanding",
            max_points,
            None,
            _d("advisories_no_publication_date"),
        )
    stale = [f for f in dated if f.oldest_advisory_days >= STALE_ADVISORY_DAYS]
    if not stale:
        return _comp(
            "No advisories left outstanding",
            max_points,
            max_points,
            _d("advisories_none_stale", days=STALE_ADVISORY_DAYS),
        )
    units = [penalty_units(f.severity, f.cvss_score) for f in stale]
    earned = max_points / (1.0 + sum(units) / ADVISORY_VOLUME_SCALE)
    oldest = max(f.oldest_advisory_days for f in stale)
    return _comp(
        "No advisories left outstanding",
        max_points,
        earned,
        _d(
            "advisories_stale",
            count=len(stale),
            days=STALE_ADVISORY_DAYS,
            oldest=oldest,
        ),
    )


def metric_dependency_advisories(data: RepoData) -> Optional[Metric]:
    """Known advisories affecting the resolved dependency set.

    Deliberately separate from ``security_posture`` rather than another
    component of it. Scorecard's own ``Vulnerabilities`` check already queries
    OSV and already contributes to posture at High risk weight; folding a
    second OSV-derived signal into the same metric would count one body of
    evidence twice. The two answer different questions: Scorecard asks whether
    the project carries known-vulnerable dependencies at all, this asks which
    ones, how severe, and fixed in which version.

    Excluded (not zero) whenever the dependency graph or the lookup was
    unavailable — a repository is never penalized for having GitHub's
    dependency graph switched off.
    """
    adv = data.dependencies.advisories
    if not adv.collected or adv.assessed_count <= 0:
        return None

    direct = [f for f in adv.findings if f.direct]
    indirect = [f for f in adv.findings if not f.direct]
    # In repository_graph scope the transitive set is contaminated: it contains
    # the project's development and test pins, which nobody installs. Scoring it
    # punished well-run projects for their own test matrices — calibration put
    # rails/rails at 16 and square/retrofit at 31 on the strength of gems and
    # jars that never ship. The *direct* set stays scorable in both scopes,
    # because "direct" means "matches a declared runtime dependency" and the
    # declared list already excludes dev/test groups.
    published_scope = adv.scope == "published_package"
    scored_findings = adv.findings if published_scope else direct
    if published_scope:
        indirect_component = _advisory_component(
            "Indirect dependencies free of known advisories", 25, indirect, "indirect"
        )
    else:
        indirect_component = _comp(
            "Indirect dependencies free of known advisories",
            25,
            None,
            _d("advisories_scope_not_separable"),
        )
    components = [
        _advisory_component("Direct dependencies free of known advisories", 35, direct, "direct"),
        indirect_component,
        _stale_advisory_component(40, scored_findings),
    ]
    # Severity counts are rendered as an inputs row, so they are flattened to a
    # readable string here; the structured counts stay in
    # ``data.dependencies.advisories.by_severity``.
    severities = ", ".join(
        f"{name} {adv.by_severity[name]}" for name in SEVERITY_ORDER if adv.by_severity.get(name)
    )
    metric = _metric(
        "dependency_advisories",
        "Dependency advisories",
        components,
        {
            "source": adv.source,
            "assessed_packages": adv.assessed_count,
            "unassessed_packages": adv.unassessed_count,
            "affected_packages": adv.affected_count,
            "direct_affected_packages": adv.direct_affected_count,
            "advisories": adv.advisory_count,
            "affected_by_severity": severities or "none",
        },
    )
    if metric is not None:
        if adv.scope == "published_package":
            subject = (
                f"Matched the {adv.assessed_package} runtime dependency closure — what "
                f"installing the published package pulls in — {adv.assessed_count} packages"
            )
            limits = " Reachability is not analyzed."
        else:
            subject = f"Matched {adv.assessed_count} resolved dependencies against OSV"
            limits = (
                " This repository publishes no package the index resolves, so the"
                " repository dependency graph was assessed instead. That graph mixes"
                " development and test pins with shipped dependencies, so only the"
                " declared runtime dependencies are scored; transitive findings are"
                " reported as context and excluded from the score."
                " Reachability is not analyzed."
            )
        coverage = (
            subject
            + (
                f"; {adv.unassessed_count} could not be assessed (no resolved version, "
                "an unsupported ecosystem, or beyond the reported package list)."
                if adv.unassessed_count
                else "."
            )
            + limits
        )
        metric.note = f"{metric.note} {coverage}" if metric.note else coverage
        if adv.scope == "published_package":
            metric.notes.append(
                _note(
                    "advisories_scope_published",
                    package=adv.assessed_package,
                    assessed=adv.assessed_count,
                )
            )
        else:
            metric.notes.append(_note("advisories_scope_repository", assessed=adv.assessed_count))
        # Same order as the prose in ``note``: what was matched, what could not
        # be, then the standing limits of the method.
        if adv.unassessed_count:
            metric.notes.append(_note("advisories_unassessed", count=adv.unassessed_count))
        if adv.scope != "published_package":
            metric.notes.append(_note("advisories_repo_graph_caveat"))
        metric.notes.append(_note("advisories_reachability"))
    return metric


# A dependency the OpenSSF corpus reports as a malicious package is not a
# severity, it is a state. There is no version to upgrade to and no partial
# exposure: an install-time payload runs on every machine that resolves the
# graph, at any depth, which is why direct and indirect are treated alike.
#
# So this scores as a policy multiplier and a ceiling, the same shape as the
# high-risk jurisdiction red flag, rather than as points off. The ceiling is
# the top of the *critical* band — one band below jurisdiction exposure's 49,
# because this is a confirmed compromise of the software rather than an
# exposure to a risk.
#
# The multiplier is set so the ceiling can actually bind: at 35% a repository
# scoring 83 or above is held at the cap, and everything below it is scaled.
# A far harsher multiplier would put every flagged repository within a few
# points of 1, which reads the same as an abandoned project and throws away
# the ordering among flagged repositories for no gain — the ceiling already
# carries the message that this is not a survivable finding.
MALICIOUS_DEPENDENCY_MULTIPLIER = 35
MALICIOUS_DEPENDENCY_OVERALL_CAP = 29


def metric_malicious_dependencies(data: RepoData) -> Optional[Metric]:
    """Dependencies reported as malicious packages, from the OpenSSF corpus.

    OSV.dev serves ``ossf/malicious-packages`` under ``MAL-`` identifiers, so
    these arrive on the batch query ``vulns.py`` already makes — the finding
    costs no additional request. ``vulns.py`` keeps them out of the advisory
    list, where they would otherwise score as "unknown" severity: the weight of
    a moderate CVE for a package that is outright malware.

    Excluded (not zero) when the lookup did not run, exactly like the advisory
    metric — a repository with GitHub's dependency graph switched off is never
    penalized for the gap.
    """
    adv = data.dependencies.advisories
    if not adv.collected or adv.assessed_count <= 0:
        return None

    findings = adv.malicious
    # A version the registry has pulled cannot be installed, so the repository
    # does not resolve to live malware however alarming the report reads. Those
    # findings stay in the report — the dependency is on a compromised name and
    # that is worth seeing — but they raise no flag and cost no points.
    #
    # The test is the *exact resolved version*, never the package's latest.
    # npm leaves an `x.y.z-security` holding package as `latest`, which protects
    # every consumer resolving a range and none that pinned the bad version;
    # reading `latest` would clear repositories that still fetch the artifact on
    # every install. ``still_published is None`` means the check could not run,
    # and an unanswered question is scored as if the package were live.
    live = [f for f in findings if f.still_published is not False]
    withdrawn = [f for f in findings if f.still_published is False]

    if not live:
        detail = (
            [_d("no_malicious_dependencies")]
            if not withdrawn
            else [_d("malicious_dependencies_withdrawn", count=len(withdrawn))]
        )
        component = _comp("No dependency reported as a malicious package", 100, 100, detail)
    else:
        listed = ", ".join(f"{f.name} {f.version or ''}".strip() for f in live[:3])
        detail = [_d("malicious_dependencies", count=len(live), packages=listed)]
        if len(live) > 3:
            detail.append(_d("malicious_dependencies_more", count=len(live) - 3))
        if withdrawn:
            detail.append(_d("malicious_dependencies_withdrawn_extra", count=len(withdrawn)))
        component = _comp(
            "No dependency reported as a malicious package",
            100,
            MALICIOUS_DEPENDENCY_MULTIPLIER,
            detail,
        )

    metric = _metric(
        "malicious_dependencies",
        "Malicious dependencies",
        [component],
        {
            "source": adv.source,
            "assessed_packages": adv.assessed_count,
            "malicious_packages": adv.malicious_count,
            "installable_malicious_packages": len(live),
            "withdrawn_malicious_packages": len(withdrawn),
            "direct_malicious_packages": sum(1 for f in live if f.direct),
            "red_flag": bool(live),
            "packages": [
                {
                    "ecosystem": f.ecosystem,
                    "name": f.name,
                    "version": f.version,
                    "direct": f.direct,
                    "advisory_ids": f.advisory_ids,
                    "still_published": f.still_published,
                }
                for f in findings
            ],
            "meaning": "reported as a malicious package by the OpenSSF corpus; the remedy "
            "is removal or moving off the compromised name, never an upgrade of the same "
            "artifact. Versions the registry has since pulled are listed but not scored",
        },
    )
    if metric is not None and live:
        metric.note = (
            f"{len(live)} resolved dependency(ies) resolve to a version the registry still "
            "serves and the OpenSSF corpus reports as malicious. Direct and indirect are "
            "treated alike: an install-time payload runs at any depth in the graph. A report "
            "here concerns the package as published, not the maintainers of this repository, "
            "which may have resolved it unknowingly."
        )
        metric.notes.append(_note("malicious_dependency_limits", count=len(live)))
    elif metric is not None and withdrawn:
        metric.note = (
            f"{len(withdrawn)} resolved dependency(ies) are reported as malicious packages, "
            "but the registry no longer serves the resolved version, so nothing installable "
            "remains. Reported for the record and not scored."
        )
        metric.notes.append(_note("malicious_dependency_withdrawn", count=len(withdrawn)))
    return metric


JURISDICTION_ROLE_MULTIPLIER: dict[str, int] = {
    # The owner controls the repository namespace and release surface.
    "owner": 20,
    # A top contributor is strong code-history evidence, but not proof of
    # current admin/maintain permissions.
    "top_contributor": 50,
    # Public organization membership is an affiliation signal only.
    "contributor_organization": 75,
}
HIGH_RISK_JURISDICTION_OVERALL_CAP = 49
# A contributor-side exposure only affects the score when the matched
# contributor carries meaningful commit weight: at least this many commits, or
# at least this share of all sampled human commits. The share leg protects the
# small-repository case, where a genuine co-maintainer may hold few absolute
# commits; the absolute leg protects the large-repository case, where a
# meaningful body of work can still be a small percentage. Measured on the
# production record (2026-08-02), 56% of contributor-driven flags came from
# people with fewer than 10 commits — drive-by exposure, not stewardship.
# Owner exposures are never gated: the owner controls the release surface at
# any commit count.
JURISDICTION_MIN_COMMITS = 50
JURISDICTION_MIN_COMMIT_SHARE = 0.10


def metric_high_risk_jurisdiction_exposure(data: RepoData) -> Optional[Metric]:
    """Supply-chain exposure from self-published profile locations.

    This is explicitly not a nationality inference. Only high-confidence
    Russia, Iran, or North Korea location matches affect the score; ambiguous
    matches remain review-only. The value is a multiplier applied to Security,
    so a clean location never raises weak security hygiene.
    """
    assessment = assess_repo(data)
    if assessment.assessed_locations == 0:
        return None

    contributors = data.maintainership.top_contributors
    # Total sampled human commits, recovered from the stored share of the top
    # contributor — the same denominator bus factor is derived from.
    share_denominator = None
    if contributors and data.maintainership.top_contributor_share:
        share_denominator = contributors[0].commits / data.maintainership.top_contributor_share
    contributor_weight: dict[str, tuple[int, Optional[float]]] = {}
    organization_weight: dict[str, tuple[int, Optional[float]]] = {}
    for contributor in contributors:
        share = contributor.commits / share_denominator if share_denominator else None
        weight = (contributor.commits, share)
        contributor_weight[contributor.login.casefold()] = weight
        organizations = contributor.profile.organizations if contributor.profile else []
        for organization in organizations:
            key = organization.login.casefold()
            # An organization exposure is as strong as its strongest carrier.
            if key not in organization_weight or contributor.commits > organization_weight[key][0]:
                organization_weight[key] = weight

    def carries_weight(exposure) -> bool:
        if exposure.role == "owner":
            return True
        table = (
            contributor_weight if exposure.role == "top_contributor" else organization_weight
        )
        commits, share = table.get(exposure.subject, (0, None))
        return commits >= JURISDICTION_MIN_COMMITS or (
            share is not None and share >= JURISDICTION_MIN_COMMIT_SHARE
        )

    scored = [e for e in assessment.exposures if carries_weight(e)]
    below_threshold = [e for e in assessment.exposures if not carries_weight(e)]

    multiplier = min(
        (JURISDICTION_ROLE_MULTIPLIER[item.role] for item in scored),
        default=100,
    )

    def aggregate(exposures) -> list[dict]:
        by_country_role: dict[tuple[str, str], int] = {}
        for exposure in exposures:
            key = (exposure.country, exposure.role)
            by_country_role[key] = by_country_role.get(key, 0) + 1
        return [
            {"country": country, "role": role, "count": count}
            for (country, role), count in sorted(by_country_role.items())
        ]

    evidence = aggregate(scored)
    below_threshold_evidence = aggregate(below_threshold)
    detail = (
        [_d("jurisdiction_no_match")]
        if not evidence
        else [
            _d(
                "jurisdiction_exposure" if i == 0 else "jurisdiction_exposure_next",
                country=row["country"],
                role=row["role"],
                count=row["count"],
            )
            for i, row in enumerate(evidence)
        ]
    )
    if below_threshold:
        detail.append(
            _d(
                "jurisdiction_below_threshold",
                count=len(below_threshold),
                commits=JURISDICTION_MIN_COMMITS,
                share=round(JURISDICTION_MIN_COMMIT_SHARE * 100),
            )
        )
    metric = _metric(
        "high_risk_jurisdiction_exposure",
        "High-Risk Jurisdiction Exposure",
        [_comp("Policy exposure multiplier", 100, multiplier, detail)],
        {
            "policy_countries": ["Russia", "Iran", "North Korea"],
            "assessed_self_published_locations": assessment.assessed_locations,
            "review_only_matches": assessment.review_matches,
            "red_flag": bool(evidence),
            "exposures": evidence,
            "below_threshold_exposures": below_threshold_evidence,
            "commit_weight_rule": {
                "min_commits": JURISDICTION_MIN_COMMITS,
                "min_commit_share": JURISDICTION_MIN_COMMIT_SHARE,
            },
            "meaning": "self-published location evidence; not nationality or citizenship",
        },
    )
    if metric is not None:
        metric.note = (
            "Only high-confidence self-published location evidence affects this multiplier. "
            "Ambiguous matches are review-only; country evidence is not proof of nationality, "
            "citizenship, legal registration, malicious intent, or sanctions status."
        )
        metric.notes.append(_note("jurisdiction_evidence_limits"))
        if below_threshold:
            metric.note += (
                f" {len(below_threshold)} match(es) from contributors below the commit-weight "
                f"threshold (fewer than {JURISDICTION_MIN_COMMITS} commits and under "
                f"{round(JURISDICTION_MIN_COMMIT_SHARE * 100)}% of sampled commits) are "
                "recorded for review and do not affect the score."
            )
            metric.notes.append(
                _note("jurisdiction_below_threshold", count=len(below_threshold))
            )
    return metric


# ---------------------------------------------------------------------------
# AI readiness metrics
#
# How well the repo is equipped to be developed and maintained with AI coding
# agents. Presence-based, file-tree signals — infrastructure exists, not how
# good it is. The category carries weight 0.0 in the overall score: it is an
# independent, additive badge that never drags a solid project's health down.
# ---------------------------------------------------------------------------


def metric_ai_agent_context(data: RepoData) -> Optional[Metric]:
    """Does the repo give AI agents guidance and machine-readable docs?"""
    ai = data.ai_readiness

    if ai.agent_instruction_files:
        substantive = (ai.agent_instruction_max_bytes or 0) >= AI_AGENT_STUB_BYTES
        pts = 45.0 if substantive else 18.0
        detail = [_d("file_list", files=", ".join(ai.agent_instruction_files))]
        if not substantive:
            detail.append(_d("agent_instructions_stub"))
        instructions = _comp("Agent instructions", 45, pts, detail)
    else:
        instructions = _comp("Agent instructions", 45, 0.0, _d("no_agent_instructions"))

    llms = _check(
        "Machine-readable docs (llms.txt)", ai.has_llms_txt, 15,
        _d("llms_txt_present") if ai.has_llms_txt else None,
    )
    history, legible_share = _legible_history(data.activity)
    return _metric(
        "ai_agent_context",
        "Agent context & guidance",
        [instructions, llms, history],
        {
            "agent_instruction_files": ai.agent_instruction_files,
            "agent_instruction_max_bytes": ai.agent_instruction_max_bytes,
            "has_llms_txt": ai.has_llms_txt,
            "legible_history_share": legible_share,
        },
    )


# A commit subject in conventional-commit form, which states type and scope
# before anything else.
_CONVENTIONAL_SUBJECT = re.compile(r"^[a-z]+(\([^)]*\))?!?:\s")
# A reference to the discussion behind the change. Deliberately only GitHub
# issue/PR forms: a general `[A-Z]{2,}-\d+` tracker-key pattern was measured and
# rejected — across twelve large projects it matched six subjects, one of them
# the false positive "WTF-16", and it would equally have matched "UTF-8",
# "SHA-256" and "ISO-8601".
_ISSUE_REFERENCE = re.compile(r"#\d+|\bGH-\d+\b")
# A body this long is an explanation rather than a restatement of the subject.
LEGIBLE_BODY_MIN_CHARS = 80
# Share of human commits carrying intent at which the history counts as legible.
LEGIBLE_HISTORY_FULL_MARKS = 0.75


def _legible_history(activity: Activity) -> tuple[MetricComponent, Optional[float]]:
    """Can an agent recover *why* a change was made from the history alone?

    Every other component of this metric asks whether someone wrote a file for
    agents to read. This one asks whether the record the project already
    produces carries intent, which is what an agent actually consults before
    editing unfamiliar code — and unlike an instruction file, every project has
    a commit history whether or not it has heard of coding agents.

    A commit counts when its subject is structured (conventional-commit form,
    or a reference to the issue or pull request behind it) *or* its body
    explains the change. Both forms are counted because either alone is
    parochial: measured over the newest 100 commits, requiring structure scored
    the Linux kernel at 18% despite its exemplary explanatory messages, while
    requiring bodies would have failed vuejs/core at 2% despite a fully
    conventional history.

    Bot commits are excluded: automated subjects are uniformly well-formed and
    would inflate the share without evidencing anything about the project's own
    practice.
    """
    humans = [c for c in activity.recent_commits if not c.is_bot]
    if len(humans) < HUMAN_COMMIT_SAMPLE_MIN:
        return _comp("Legible commit history", 40, None), None

    def carries_intent(commit: CommitRecord) -> bool:
        headline = commit.headline or ""
        if _CONVENTIONAL_SUBJECT.match(headline) or _ISSUE_REFERENCE.search(headline):
            return True
        return len((commit.body or "").strip()) >= LEGIBLE_BODY_MIN_CHARS

    legible = sum(1 for c in humans if carries_intent(c))
    share = legible / len(humans)
    points = min(40.0, 40.0 * share / LEGIBLE_HISTORY_FULL_MARKS)
    detail = _d("legible_history", legible=legible, sampled=len(humans))
    return _comp("Legible commit history", 40, points, detail), round(share, 3)


def metric_ai_verify_loop(data: RepoData) -> Optional[Metric]:
    """Can an agent set up, run, and verify a change autonomously?

    The single most important dimension for autonomous agents — hence the
    highest weight in the category. Reuses the canonical test/lint/lockfile
    signals plus AI-specific bootstrap, type-check and container signals.
    """
    ai = data.ai_readiness
    q = data.quality_signals
    lockfiles = data.security_signals.lockfiles

    # A dedicated task runner earns full marks; a toolchain that defines the
    # command itself earns most of them. Crediting only the runner taxed whole
    # ecosystems for having better defaults — measured, rust-lang/regex and
    # serde scored zero here while `cargo test` is the canonical verify loop.
    if ai.bootstrap_files:
        bootstrap = _comp(
            "One-command bootstrap", 18, 18.0, _files(ai.bootstrap_files)
        )
    elif ai.toolchain_manifests:
        bootstrap = _comp(
            "One-command bootstrap", 18, 12.6,
            _d("toolchain_convention", files=", ".join(ai.toolchain_manifests[:3])),
        )
    else:
        bootstrap = _comp("One-command bootstrap", 18, 0.0)

    tests = _check("Automated tests", q.has_tests, 22)
    lint = _check(
        "Lint / format config", q.has_linter_config, 11, _files(q.linter_configs)
    )

    typed_language = data.repo.primary_language in STATICALLY_TYPED_LANGUAGES
    has_typecheck = bool(ai.typecheck_configs) or typed_language
    typecheck_detail = (
        _files(ai.typecheck_configs) if ai.typecheck_configs
        else _d("statically_typed_language", language=data.repo.primary_language)
        if typed_language
        else None
    )
    typecheck = _check("Static type checking", has_typecheck, 11, typecheck_detail)

    repro_bits = []
    if ai.has_devcontainer:
        repro_bits.append("devcontainer")
    if ai.has_dockerfile:
        repro_bits.append("Dockerfile")
    if ai.has_nix:
        repro_bits.append("Nix")
    if lockfiles:
        repro_bits.append("lockfile")
    repro = _check("Reproducible environment", bool(repro_bits), 10, _files(repro_bits))

    demonstrated, agent_share = _demonstrated_agent_practice(data.activity)
    automation, dep_bot_share = _automated_maintenance(
        data.activity, data.security_signals.has_dependabot_config
    )

    return _metric(
        "ai_verify_loop",
        "Verify loop (build / test / typecheck)",
        [
            bootstrap, tests, lint, typecheck, repro, demonstrated, automation,
            _scorecard_evidence(data, "Pinned-Dependencies", 10),
        ],
        {
            "bootstrap_files": ai.bootstrap_files,
            "toolchain_manifests": ai.toolchain_manifests,
            "has_tests": q.has_tests,
            "has_linter_config": q.has_linter_config,
            "typecheck_configs": ai.typecheck_configs,
            "typed_language": typed_language,
            "has_devcontainer": ai.has_devcontainer,
            "has_dockerfile": ai.has_dockerfile,
            "has_nix": ai.has_nix,
            "lockfiles": lockfiles,
            "agent_commit_share": agent_share,
            "dependency_bot_commit_share": dep_bot_share,
        },
    )


def _automated_maintenance(
    activity: Activity, has_bot_config: bool
) -> tuple[MetricComponent, Optional[float]]:
    """Do machine-authored changes actually land in this repository?

    A dependency bot's pull request is a machine-authored change that has to
    clear the same gates an agent's would: the tests run, the checks pass, the
    change merges. A project already absorbing that traffic has demonstrated
    the pathway an agent needs, which is why this sits beside demonstrated
    agent practice rather than being folded into it — the two are different
    evidence, and either can be present without the other.

    Note the deliberate asymmetry with ``development_activity``, where the same
    bot commits *discount* the score. The two metrics ask different questions:
    Vitality asks whether people are still maintaining this, AI Readiness asks
    whether machines can. A repository can honestly answer yes to one and no to
    the other, and the same fact is evidence for both answers.

    Observed commits outrank configuration: a ``dependabot.yml`` can sit in a
    repository with the integration switched off, and Renovate is routinely
    configured outside the repository altogether — measured, prettier and
    vuejs/core run it across a third of their commits while carrying no
    recognizable config file at all.
    """
    commits = activity.recent_commits
    bot_commits = [c for c in commits if is_dependency_bot(c.author_login)]

    if bot_commits:
        share = len(bot_commits) / len(commits)
        detail = _d(
            "dependency_bot_commits", count=len(bot_commits), sampled=len(commits)
        )
        return _comp("Automated maintenance", 8, 8.0, detail), round(share, 3)

    if has_bot_config:
        # Configured, but nothing in the sampled window to show for it.
        return _comp("Automated maintenance", 8, 5.0, _d("dependency_bot_config_only")), 0.0

    if len(commits) < HUMAN_COMMIT_SAMPLE_MIN:
        # No sample and no config: nothing was observed either way.
        return _comp("Automated maintenance", 8, None), None

    return _comp("Automated maintenance", 8, 0.0, _d("no_dependency_automation")), 0.0


# Share of the commit sample carrying agent authorship at which the loop counts
# as demonstrated. Measured over the newest 100 commits of large projects:
# react 13%, axios 6%, prettier 5%, and zero almost everywhere else. The bar is
# set where a project has clearly adopted the practice rather than tried it once.
AGENT_COMMIT_SHARE_FULL_MARKS = 0.05


def _demonstrated_agent_practice(activity: Activity) -> tuple[MetricComponent, Optional[float]]:
    """Evidence that agents actually land changes in this repository.

    Every other component of this metric reads the file tree: a task runner
    exists, a lockfile exists. They are proxies for a loop nobody has observed
    running. Commits an agent authored — or a maintainer credited an agent for
    — are the one outcome signal available, and they say the loop was closed at
    least once in practice.

    This evidences adoption, not autonomy: a maintainer driving an agent
    interactively produces the same trailer as an unattended run, and the two
    cannot be told apart from public data. Weighted accordingly.

    Excluded (weights renormalized) when no commit sample was collected, so an
    unauthenticated scan is never marked down for what it could not observe.
    Detection of agents is a floor — see bots.py — so absence of evidence is
    read as absence of evidence, never as proof the loop is broken.
    """
    commits = activity.recent_commits
    if len(commits) < HUMAN_COMMIT_SAMPLE_MIN:
        return _comp("Demonstrated agent practice", 10, None), None

    agent_commits = [c for c in commits if c.is_coding_agent]
    share = len(agent_commits) / len(commits)
    points = min(10.0, 10.0 * share / AGENT_COMMIT_SHARE_FULL_MARKS)
    detail = (
        _d("agent_authored_commits", count=len(agent_commits), sampled=len(commits))
        if agent_commits
        else _d("no_agent_authored_commits", sampled=len(commits))
    )
    return _comp("Demonstrated agent practice", 10, points, detail), round(share, 3)


def metric_ai_code_legibility(data: RepoData) -> Optional[Metric]:
    """Is the code legible to a model? (typed, and no giant files)

    ``None`` for repos with no detected source files (docs-only, etc.), so
    they are never penalized for a dimension that does not apply."""
    ai = data.ai_readiness
    lang = data.repo.primary_language

    if lang is not None:
        typed_language = lang in STATICALLY_TYPED_LANGUAGES
        if typed_language:
            type_pts = 45.0
            type_detail = _d("statically_typed_language", language=lang)
        elif ai.typecheck_configs:
            type_pts = 27.0
            type_detail = _d(
                "typecheck_config_language",
                language=lang,
                files=", ".join(ai.typecheck_configs),
            )
        else:
            type_pts = 0.0
            type_detail = _d("no_typecheck_config_language", language=lang)
        typing = _comp("Type-checkable code", 45, type_pts, type_detail)
    else:
        typing = _comp("Type-checkable code", 45, None, _d("primary_language_unknown"))

    if ai.source_files_sampled > 0:
        share_ok = 1.0 - ai.oversized_source_files / ai.source_files_sampled
        file_sizes = _comp(
            "Manageable file sizes", 55, share_ok * 55.0,
            _d(
                "oversized_source_files",
                oversized=ai.oversized_source_files,
                sampled=ai.source_files_sampled,
                kb=AI_OVERSIZED_SOURCE_BYTES // 1000,
            ),
        )
    else:
        file_sizes = _comp("Manageable file sizes", 55, None, _d("no_source_files"))

    metric = _metric(
        "ai_code_legibility",
        "Code legibility for models",
        [typing, file_sizes],
        {
            "primary_language": lang,
            "source_files_sampled": ai.source_files_sampled,
            "oversized_source_files": ai.oversized_source_files,
            "largest_source_bytes": ai.largest_source_bytes,
        },
    )
    return metric


def metric_ai_interfaces(data: RepoData) -> Optional[Metric]:
    """Does the repo expose machine-readable interfaces and runnable examples?

    ``None`` when the repo exposes none of these — a plain library legitimately
    has no API schema, so absence is treated as not-applicable (excluded and
    renormalized), never as a penalty."""
    ai = data.ai_readiness
    if not (ai.api_schema_files or ai.has_mcp_signal or ai.example_dirs):
        return None

    schema = _check(
        "API schema (OpenAPI/GraphQL/proto)", bool(ai.api_schema_files), 40,
        _files(ai.api_schema_files),
    )
    mcp = _check("MCP server", ai.has_mcp_signal, 20)
    examples = _check(
        "Runnable examples", bool(ai.example_dirs), 40, _files(ai.example_dirs)
    )
    return _metric(
        "ai_interfaces",
        "Machine-readable interfaces",
        [schema, mcp, examples],
        {
            "api_schema_files": ai.api_schema_files,
            "has_mcp_signal": ai.has_mcp_signal,
            "example_dirs": ai.example_dirs,
        },
    )


# ---------------------------------------------------------------------------
# Category definitions & rollup
# ---------------------------------------------------------------------------


class CategorySpec:
    """One scoring category: a weighted group of metrics."""

    def __init__(
        self,
        key: str,
        name: str,
        description: str,
        weight: float,
        metrics: dict[str, tuple[float, Callable[[Any], Optional[Metric]]]],
        rollup: str = "weighted_mean",
    ):
        self.key = key
        self.name = name
        self.description = description
        self.weight = weight
        # metric_key -> (weight within category, compute function)
        self.metrics = metrics
        self.rollup = rollup


# Repository categories. Category weights sum to 1.0.
REPO_CATEGORIES: list[CategorySpec] = [
    CategorySpec(
        "vitality", "Vitality",
        "Is the project alive — is code being written and are releases shipping?",
        0.22,
        {
            "development_activity": (0.6, metric_development_activity),
            "release_discipline": (0.4, metric_release_discipline),
            # Zero additive weight: a maintained project earns nothing for
            # being maintained — the two metrics above already measure that.
            # This is applied as a multiplier on the overall score, because an
            # abandoned project's other categories stop describing anything a
            # consumer can rely on.
            "abandonment": (0.0, metric_abandonment),
        },
    ),
    CategorySpec(
        "community", "Community & Adoption",
        "Does the project have users, downloads, attention, and a welcoming setup for contributors?",
        0.18,
        {
            "popularity": (0.4, metric_popularity),
            "community_health": (0.35, metric_community_health),
            "ecosystem_adoption": (0.25, metric_ecosystem_adoption),
        },
    ),
    CategorySpec(
        "governance", "Sustainability & Governance",
        "Will the project survive its people — bus factor, responsiveness, who backs it, and package upkeep?",
        0.24,
        {
            "maintainer_resilience": (0.3, metric_maintainer_resilience),
            "responsiveness": (0.25, metric_responsiveness),
            "stewardship": (0.25, metric_stewardship),
            "package_maintenance": (0.2, metric_package_maintenance),
        },
    ),
    CategorySpec(
        "engineering", "Engineering Quality",
        "Are baseline engineering and documentation practices in place?",
        0.20,
        {
            "engineering_practices": (0.6, metric_engineering_practices),
            "documentation": (0.4, metric_documentation),
        },
    ),
    CategorySpec(
        "security", "Security",
        "Are visible security and supply-chain practices strong, with no malicious dependency "
        "and no unresolved high-risk jurisdiction exposure?",
        0.16,
        {
            # Posture and advisories are the additive pair; jurisdiction carries
            # no additive weight because it is a multiplier on the result.
            #
            # Advisories sit at 0.2, not 0.3. Calibration against sixteen
            # popular packages found fifteen scoring exactly 100 — well-run
            # projects genuinely do not ship known-vulnerable dependencies — so
            # at 0.3 the metric handed out a mean +9.2 points of Security for
            # free, and the largest lifts went to the projects with the weakest
            # posture. A near-constant signal must not outweigh the one that
            # actually varies.
            "security_posture": (0.8, metric_security_posture),
            "dependency_advisories": (0.2, metric_dependency_advisories),
            # Both red flags carry zero additive weight: neither is a reward for
            # a clean result, and both are applied as multipliers afterwards.
            "malicious_dependencies": (0.0, metric_malicious_dependencies),
            "high_risk_jurisdiction_exposure": (0.0, metric_high_risk_jurisdiction_exposure),
        },
        rollup="adjusted_security",
    ),
    CategorySpec(
        "ai_readiness", "AI Readiness",
        "How well is the repo equipped to be developed and maintained with AI "
        "coding agents? An independent, experimental badge — weight 0.0, so it "
        "is surfaced on its own and does not affect the overall health score.",
        0.0,
        {
            "ai_agent_context": (0.30, metric_ai_agent_context),
            "ai_verify_loop": (0.40, metric_ai_verify_loop),
            "ai_code_legibility": (0.15, metric_ai_code_legibility),
            "ai_interfaces": (0.15, metric_ai_interfaces),
        },
    ),
]


def _rollup(value_by_key: dict[str, int], weights: dict[str, float]) -> Optional[int]:
    available = {k: v for k, v in value_by_key.items() if k in weights}
    if not available:
        return None
    total = sum(weights[k] for k in available)
    # All available items carry zero weight (e.g. only the weight-0 AI Readiness
    # category is present) — no weighted mean is defined, so report no score.
    if total <= 0:
        return None
    return max(1, min(100, round(sum(v * weights[k] for k, v in available.items()) / total)))


def _category_rollup(spec: CategorySpec, values: dict[str, int]) -> Optional[int]:
    if spec.rollup == "adjusted_security":
        # Both red-flag policies are already applied to Security posture before
        # category assembly; neither is an additive reward, so both are excluded
        # here and their weights are 0. Posture and dependency advisories
        # combine as a weighted mean under the ordinary missing-data rule: a
        # repository with no dependency graph scores on posture alone rather
        # than being penalized for the gap.
        additive = {
            key: weight
            for key, (weight, _) in spec.metrics.items()
            if key not in ("high_risk_jurisdiction_exposure", "malicious_dependencies")
        }
        return _rollup(values, additive)
    weights = {key: weight for key, (weight, _) in spec.metrics.items()}
    return _rollup(values, weights)


def _build(
    specs: list[CategorySpec],
    computed: dict[str, Optional[Metric]],
    overall_name: str,
    config: ScanConfig,
) -> tuple[Optional[Metric], list[MetricCategory]]:
    """Assemble categories and the overall score from computed metrics.

    Categories disabled by ``config`` are dropped entirely (not rendered, not
    scored); categories with no scorable metric are dropped as before."""
    categories: list[MetricCategory] = []
    cat_values: dict[str, int] = {}
    cat_weights: dict[str, float] = {}

    for spec in specs:
        if not config.category_enabled(spec.key):
            continue
        present = [
            computed[k] for k in spec.metrics if computed.get(k) is not None
        ]
        if not present:
            continue
        value = _category_rollup(spec, {m.key: m.value for m in present})
        if value is None:
            continue
        categories.append(
            MetricCategory(
                key=spec.key,
                name=spec.name,
                description=spec.description,
                weight=spec.weight,
                value=value,
                band=band_for(value) if value is not None else None,
                metrics=present,
            )
        )
        # Weight-0 categories (e.g. AI Readiness) are rendered on their own but
        # excluded from the overall score and its inputs/notes — they are
        # independent, additive badges, not part of the health rollup.
        if value is not None and spec.weight > 0:
            cat_values[spec.key] = value
            cat_weights[spec.key] = spec.weight

    overall_value = _rollup(cat_values, cat_weights)
    if overall_value is None:
        return None, categories
    disabled = [s.name for s in specs if not config.category_enabled(s.key)]
    no_data = [
        s.name for s in specs
        if config.category_enabled(s.key) and s.weight > 0 and s.key not in cat_values
    ]
    note_parts: list[str] = []
    notes: list[MetricNote] = []
    if no_data:
        note_parts.append(f"Categories without data excluded: {', '.join(no_data)}.")
        notes.append(_note("categories_no_data", categories=_category_keys(specs, no_data)))
    if disabled:
        note_parts.append(f"Categories disabled in scan configuration: {', '.join(disabled)}.")
        notes.append(_note("categories_disabled", categories=_category_keys(specs, disabled)))
    if disabled or no_data:
        note_parts.append("Weights renormalized over the remaining categories.")
        notes.append(_note("category_weights_renormalized"))
    overall = Metric(
        key="overall",
        name=overall_name,
        value=overall_value,
        band=band_for(overall_value),
        notes=notes,
        inputs={k: v for k, v in cat_values.items()},
        note=" ".join(note_parts) if note_parts else None,
    )
    return overall, categories


def _category_keys(specs: list["CategorySpec"], names: list[str]) -> list[str]:
    """Category keys for the given display names, in the order given."""
    by_name = {spec.name: spec.key for spec in specs}
    return [by_name.get(n, _slug(n)) for n in names]


def _compute(
    specs: list[CategorySpec], data: Any, config: ScanConfig
) -> dict[str, Optional[Metric]]:
    """Run each metric function, honoring the scan configuration.

    Disabled categories and metrics are skipped (recorded as None); enabled
    metrics have their configuration-disabled components switched off."""
    computed: dict[str, Optional[Metric]] = {}
    for spec in specs:
        category_on = config.category_enabled(spec.key)
        for key, (_, fn) in spec.metrics.items():
            if not category_on or not config.metric_enabled(key):
                computed[key] = None
                continue
            metric = fn(data)
            if metric is not None:
                metric = _disable_components(metric, config.disabled_component_names(key))
            computed[key] = metric
    return computed


def _apply_malicious_policy(metric: Metric, malware: Metric, scope: str) -> None:
    """Apply the malicious-dependency multiplier and ceiling to one score.

    ``scope`` is ``"posture"`` or ``"overall"`` and only selects the wording;
    the arithmetic is identical. Called only when this policy is the strictest
    of those that fired — see ``_strictest`` — so it never multiplies a value
    another policy has already reduced.
    """
    posture_scope = scope == "posture"
    before_key = "security_posture_before_malicious" if posture_scope else "weighted_overall_before_malicious"
    subject = "Security posture" if posture_scope else "weighted overall health"

    before = metric.value
    multiplied = max(1, round(before * malware.value / 100))
    metric.value = min(multiplied, MALICIOUS_DEPENDENCY_OVERALL_CAP)
    metric.band = band_for(metric.value)
    metric.inputs = {
        **metric.inputs,
        before_key: before,
        "malicious_dependency_multiplier": malware.value,
        "malicious_dependency_cap": MALICIOUS_DEPENDENCY_OVERALL_CAP,
    }
    adjustment_note = (
        f"A dependency reported as a malicious package applies a {malware.value}% "
        f"multiplier to {subject} and gives it a Critical ceiling of "
        f"{MALICIOUS_DEPENDENCY_OVERALL_CAP}."
    )
    metric.note = f"{metric.note} {adjustment_note}" if metric.note else adjustment_note
    metric.notes.append(
        _note(
            f"malicious_{scope}_adjustment",
            pct=malware.value,
            cap=MALICIOUS_DEPENDENCY_OVERALL_CAP,
        )
    )


def _apply_abandonment_policy(overall: Metric, abandoned: Metric) -> None:
    """Apply the abandonment multiplier, and its ceiling where the state has one.

    Applied last, after both Security red flags, and on the same terms: the
    multiplier is the metric's own value, and it never lifts a score. Unlike
    those two, the ceiling is conditional — ``at_risk`` states a concern and
    multiplies, but does not put a ceiling on a project that may well come
    back. Only the two states that assert the project is gone do that.
    """
    before = overall.value
    factor = abandoned.value
    cap = abandoned.inputs.get("cap")
    multiplied = max(1, round(before * factor / 100))
    overall.value = min(multiplied, cap) if cap else multiplied
    overall.band = band_for(overall.value)
    overall.inputs = {
        **overall.inputs,
        "weighted_overall_before_abandonment": before,
        "abandonment_state": abandoned.inputs.get("state"),
        "abandonment_multiplier": factor,
        "overall_after_abandonment_multiplier": multiplied,
        "abandonment_cap": cap,
    }
    ceiling = f" and gives it a ceiling of {cap}" if cap else ""
    adjustment_note = (
        f"Abandonment Policy applies a {factor}% multiplier to weighted overall "
        f"health{ceiling}."
    )
    overall.note = f"{overall.note} {adjustment_note}" if overall.note else adjustment_note
    overall.notes.append(_note("abandonment_overall_adjustment", pct=factor, cap=cap or 0))


def _apply_jurisdiction_policy(metric: Metric, exposure: Metric, scope: str) -> None:
    """Apply the high-risk jurisdiction multiplier and ceiling to one score."""
    posture_scope = scope == "posture"
    before_key = (
        "security_posture_before_jurisdiction" if posture_scope
        else "weighted_overall_before_jurisdiction"
    )
    after_key = (
        "security_posture_after_multiplier" if posture_scope
        else "overall_after_jurisdiction_multiplier"
    )
    before = metric.value
    multiplied = max(1, round(before * exposure.value / 100))
    metric.value = min(multiplied, HIGH_RISK_JURISDICTION_OVERALL_CAP)
    metric.band = band_for(metric.value)
    metric.inputs = {
        **metric.inputs,
        before_key: before,
        "high_risk_jurisdiction_multiplier": exposure.value,
        after_key: multiplied,
        "high_risk_jurisdiction_cap": HIGH_RISK_JURISDICTION_OVERALL_CAP,
    }
    subject = "Security posture" if posture_scope else "weighted overall health"
    adjustment_note = (
        f"High-Risk Jurisdiction Policy applies a {exposure.value}% multiplier "
        f"to {subject} and gives it an At risk ceiling of "
        f"{HIGH_RISK_JURISDICTION_OVERALL_CAP}."
    )
    metric.note = f"{metric.note} {adjustment_note}" if metric.note else adjustment_note
    metric.notes.append(
        _note(
            f"jurisdiction_{scope}_adjustment",
            pct=exposure.value,
            cap=HIGH_RISK_JURISDICTION_OVERALL_CAP,
        )
    )


def _strictest(policies: list[tuple[int, int, Callable[[], None]]]) -> None:
    """Apply only the harshest policy of those that fired.

    Red flags used to compound: each multiplied whatever the previous one left,
    so a repository carrying two landed at the product of both and one carrying
    three at the product of all three — 0.35 × 0.60 put a real project at 11
    out of a weighted 50, a number no policy chose and none could be pointed at
    to explain.

    Multipliers are severity statements, not costs to be summed. The gravest
    finding therefore governs alone and the rest are reported without moving
    the number twice. Ordered by multiplier, then by ceiling for equal
    multipliers, so the strictest of a tie still wins.
    """
    if policies:
        min(policies, key=lambda p: (p[0], p[1]))[2]()


def compute_metrics(data: RepoData, config: Optional[ScanConfig] = None) -> Metrics:
    config = config or ScanConfig()
    computed = _compute(REPO_CATEGORIES, data, config)
    exposure = computed.get("high_risk_jurisdiction_exposure")
    malware = computed.get("malicious_dependencies")
    abandoned = computed.get("abandonment")
    posture = computed.get("security_posture")

    def fired(metric: Optional[Metric]) -> bool:
        return metric is not None and metric.inputs.get("red_flag") is True

    # Security posture answers to the two flags that are statements about
    # security; abandonment is a statement about the project as a whole and
    # only reaches the health index.
    if posture is not None:
        candidates: list[tuple[int, int, Callable[[], None]]] = []
        if fired(exposure):
            candidates.append(
                (exposure.value, HIGH_RISK_JURISDICTION_OVERALL_CAP,
                 lambda: _apply_jurisdiction_policy(posture, exposure, "posture"))
            )
        if fired(malware):
            candidates.append(
                (malware.value, MALICIOUS_DEPENDENCY_OVERALL_CAP,
                 lambda: _apply_malicious_policy(posture, malware, "posture"))
            )
        _strictest(candidates)

    overall, categories = _build(REPO_CATEGORIES, computed, "Overall health", config)
    if overall is not None:
        candidates = []
        if fired(exposure):
            candidates.append(
                (exposure.value, HIGH_RISK_JURISDICTION_OVERALL_CAP,
                 lambda: _apply_jurisdiction_policy(overall, exposure, "overall"))
            )
        if fired(malware):
            candidates.append(
                (malware.value, MALICIOUS_DEPENDENCY_OVERALL_CAP,
                 lambda: _apply_malicious_policy(overall, malware, "overall"))
            )
        if fired(abandoned):
            candidates.append(
                (abandoned.value, abandoned.inputs.get("cap") or 100,
                 lambda: _apply_abandonment_policy(overall, abandoned))
            )
        _strictest(candidates)
    return Metrics(metrics_version=METRICS_VERSION, overall=overall, categories=categories)


# ---------------------------------------------------------------------------
# Organization metrics
# ---------------------------------------------------------------------------


def metric_org_profile(data: OrgData) -> Optional[Metric]:
    """Is the organization profile complete and accountable?"""
    info = data.info
    checklist = [
        _check("Verified domain", info.is_verified, 25),
        _check("Description", bool(info.description), 20),
        _check("Homepage", bool(info.blog), 15, info.blog or None),
        _check("Display name", bool(info.name), 10),
        _check("Location", bool(info.location), 10),
        _check("Contact email", bool(info.email), 10),
        _check("Social profile", bool(info.twitter_username), 10),
    ]
    return _metric(
        "profile_completeness",
        "Profile completeness",
        checklist,
        {
            "is_verified": info.is_verified,
            "description": bool(info.description),
            "blog": info.blog,
            "name": info.name,
            "location": info.location,
            "email": bool(info.email),
            "twitter_username": info.twitter_username,
        },
    )


def metric_org_portfolio(data: OrgData) -> Optional[Metric]:
    """Is the organization's repository portfolio actively maintained?"""
    p = data.portfolio
    info = data.info

    if p.repos_sampled > 0 and p.repos_pushed_90d is not None:
        ratio = p.repos_pushed_90d / p.repos_sampled
        recent = _comp(
            "Recently active repos", 50, ratio * 50.0,
            _d("org_repos_pushed_90d", count=p.repos_pushed_90d, sampled=p.repos_sampled),
        )
    else:
        recent = _comp("Recently active repos", 50, None)

    if p.repos_sampled > 0 and p.repos_pushed_365d is not None:
        ratio = p.repos_pushed_365d / p.repos_sampled
        yearly = _comp(
            "Yearly active repos", 25, ratio * 25.0,
            _d("org_repos_pushed_year", count=p.repos_pushed_365d, sampled=p.repos_sampled),
        )
    else:
        yearly = _comp("Yearly active repos", 25, None)

    size = _comp(
        "Portfolio size", 15, _log_points(info.public_repos, 15, 100),
        _d("org_public_repositories", count=info.public_repos),
    )

    if p.repos_sampled > 0:
        ratio = p.original_repos_sampled / p.repos_sampled
        original = _comp(
            "Original work", 10, ratio * 10.0,
            _d("org_original_repos", count=p.original_repos_sampled, sampled=p.repos_sampled),
        )
    else:
        original = _comp("Original work", 10, None)

    return _metric(
        "portfolio_activity",
        "Portfolio activity",
        [recent, yearly, size, original],
        {
            "public_repos": info.public_repos,
            "repos_sampled": p.repos_sampled,
            "repos_pushed_90d": p.repos_pushed_90d,
            "repos_pushed_365d": p.repos_pushed_365d,
            "forks_sampled": p.forks_sampled,
        },
    )


def metric_org_reach(data: OrgData) -> Optional[Metric]:
    """Does the organization have community traction?"""
    info = data.info
    p = data.portfolio
    followers = _comp(
        "Followers", 50, _log_points(info.followers, 50, 1000),
        _d("org_followers", count=info.followers),
    )
    stars = _comp(
        "Stars across repositories", 50, _log_points(p.total_stars_sampled, 50, 10000),
        _d("org_stars_across_repos", count=p.total_stars_sampled, sampled=p.repos_sampled),
    )
    return _metric(
        "community_reach",
        "Community reach",
        [followers, stars],
        {
            "followers": info.followers,
            "total_stars_sampled": p.total_stars_sampled,
            "repos_sampled": p.repos_sampled,
        },
    )


# Organization categories. Category weights sum to 1.0.
ORG_CATEGORIES: list[CategorySpec] = [
    CategorySpec(
        "activity_reach", "Activity & Reach",
        "Is the repository portfolio maintained and does the org have traction?",
        0.75,
        {
            "portfolio_activity": (0.6, metric_org_portfolio),
            "community_reach": (0.4, metric_org_reach),
        },
    ),
    CategorySpec(
        "governance", "Governance & Profile",
        "Is the organization accountable and clearly presented?",
        0.25,
        {"profile_completeness": (1.0, metric_org_profile)},
    ),
]


def compute_org_metrics(data: OrgData, config: Optional[ScanConfig] = None) -> OrgMetrics:
    config = config or ScanConfig()
    computed = _compute(ORG_CATEGORIES, data, config)
    overall, categories = _build(ORG_CATEGORIES, computed, "Overall organization health", config)
    return OrgMetrics(metrics_version=METRICS_VERSION, overall=overall, categories=categories)


# ---------------------------------------------------------------------------
# Configuration registry & validation
# ---------------------------------------------------------------------------

# Repository and organization methodologies share the same key namespace for
# validation — a repository-only key is still "known" when scanning an org.
ALL_CATEGORIES: list[CategorySpec] = REPO_CATEGORIES + ORG_CATEGORIES


def known_category_keys() -> set[str]:
    """Every category key the methodology defines (repositories + organizations)."""
    return {c.key for c in ALL_CATEGORIES}


def known_metric_keys() -> set[str]:
    """Every metric key the methodology defines (repositories + organizations)."""
    return {k for c in ALL_CATEGORIES for k in c.metrics}


def validate_config(config: ScanConfig) -> list[str]:
    """Return human-readable warnings for keys the methodology doesn't define.

    Category and metric keys are checked against the combined methodology.
    Component names are scoped to their metric and are not strictly validated —
    an unrecognized component name simply has no effect (only the parent metric
    key is checked)."""
    warnings: list[str] = []
    categories, metrics = known_category_keys(), known_metric_keys()
    for key in config.disabled_categories:
        if key not in categories:
            warnings.append(f"unknown category '{key}' in scan configuration (ignored)")
    for key in config.disabled_metrics:
        if key not in metrics:
            warnings.append(f"unknown metric '{key}' in scan configuration (ignored)")
    for key in config.disabled_components:
        if key not in metrics:
            warnings.append(
                f"disabled_components references unknown metric '{key}' (ignored)"
            )
    return warnings
