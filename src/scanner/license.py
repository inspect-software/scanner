"""Resolve a repository's license into one state, from every source we have.

Three independent signals answer overlapping questions, and before this module
each consumer picked whichever one was nearest:

- GitHub's ``license.spdx_id`` on the repository object — *which* license, but
  only when GitHub recognizes the text. An unrecognized file yields the
  sentinel ``NOASSERTION``, which is emphatically **not** "no license".
- GitHub's community profile ``files.license`` — *whether* a license file
  exists, with no opinion on its contents.
- OpenSSF Scorecard's ``License`` check — its own graded look for a license
  file, which disagrees with the community profile on a small but real number
  of repositories (roughly 1%).

The states are the three the data actually produces. Presence is a logical OR:
a file one source can see and another cannot is still a file, so ``absent``
requires that nothing anywhere found one. Anything weaker would let a single
flaky endpoint tell a maintainer their license is missing.

Deliberately absent is an ``unknown`` state. It would need a reliable "we could
not look" signal, and neither GitHub source distinguishes "no license" from "no
answer" — inventing the state without that signal would be false precision.
"""

from __future__ import annotations

from typing import Optional

from .models import LicenseInfo, LicenseState, RepoData

# GitHub's marker for "there is a license file, but it is not one we recognize".
# Treating this as a missing license is the bug this module exists to prevent.
NOASSERTION = "NOASSERTION"


def normalize_spdx(raw: Optional[str]) -> Optional[str]:
    """The SPDX identifier if GitHub actually recognized one, else None."""
    if not raw or raw == NOASSERTION:
        return None
    return raw


def resolve_license(
    *,
    raw_spdx: Optional[str],
    profile_has_license: bool,
    scorecard_license_score: Optional[float] = None,
) -> LicenseInfo:
    """Combine every license signal into one state.

    ``scorecard_license_score`` is Scorecard's 0..10 grade for its ``License``
    check, or None when no Scorecard ran. Any score above zero means Scorecard
    found a license file.
    """
    spdx = normalize_spdx(raw_spdx)
    scorecard_found = scorecard_license_score is not None and scorecard_license_score > 0
    # A file the repository object itself named counts too: GitHub only emits a
    # license object (even NOASSERTION) when it found something to classify.
    file_present = profile_has_license or scorecard_found or bool(raw_spdx)

    state: LicenseState
    if spdx is not None:
        state = "standard"
    elif file_present:
        state = "custom"
    else:
        state = "absent"

    return LicenseInfo(
        state=state,
        spdx_id=spdx,
        raw_spdx=raw_spdx,
        file_present=file_present,
        profile_has_license=profile_has_license,
        scorecard_found=scorecard_found if scorecard_license_score is not None else None,
    )


def scorecard_license_score(data: RepoData) -> Optional[float]:
    """Scorecard's grade for its ``License`` check, or None if it did not run."""
    scorecard = data.security_signals.scorecard
    if scorecard is None:
        return None
    check = next((item for item in scorecard.checks if item.name == "License"), None)
    return check.score if check is not None else None


def license_for_report(data: RepoData) -> LicenseInfo:
    """Resolve from a report's own fields, whatever schema version wrote it.

    Reports written before schema 0.13.0 carry no ``data.license`` block and no
    ``license_spdx_raw``, so replaying one must re-derive the state rather than
    read the default. Deriving it from the raw inputs every time — instead of
    trusting a stored state — means old and new reports go through identical
    logic, and there is still exactly one place that decides.
    """
    return resolve_license(
        # Pre-0.13.0 reports only kept the filtered identifier. That loses the
        # custom/absent distinction for repositories whose only signal was
        # NOASSERTION, which the community-profile flag below then recovers.
        raw_spdx=data.repo.license_spdx_raw or data.repo.license_spdx,
        profile_has_license=data.community.has_license,
        scorecard_license_score=scorecard_license_score(data),
    )
