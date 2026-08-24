"""People who objected to being processed, applied before anything is scored.

GDPR Art. 21 gives anyone whose data is processed under legitimate interest an
almost unconditional right to object, and the privacy policy promises that an
objection removes the enriched profile fields *and keeps them out of future
scans*. A deletion that the next scan silently undoes is not compliance with
that right, so the exclusion has to live in the pipeline rather than in a
one-off `UPDATE` — which is what this module is.

Two kinds of subject:

* **A GitHub login.** Two places hold self-published personal facts under a
  login. The displayed top contributors carry an optional ``profile`` block —
  name, location, company, public organization memberships — and the
  repository's ``owner`` carries the same kind of fields directly. Both feed
  the jurisdiction signal (``jurisdiction._located_subjects`` reads the owner
  and each contributor), so suppression clears both. What stays is the login
  itself and the commit count: they are the repository's own public history,
  the record's subject matter, and removing them would misstate who wrote the
  software.
* **An email address.** Maintainer contact channels (``data.contacts``) never
  reach the public report, but they are personal data and a maintainer may
  object to holding them at all. Suppression drops every channel carrying that
  address.

Applied at one point in ``collect.scan_repository`` — after collection,
*before* ``compute_metrics`` — so a suppressed location can neither be scored
into a jurisdiction exposure nor be written into the stored report. Applying it
after scoring would leave the objection half-honoured: invisible, but still
reflected in a number.

Matching is case-insensitive and exact. No prefixes, no domains, no wildcards:
a suppression list that can accidentally match a whole company is a way to
quietly lose data nobody asked to lose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .models import RepoData


@dataclass(frozen=True)
class Suppression:
    """Logins and email addresses that must not be retained."""

    logins: frozenset[str] = frozenset()
    emails: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls,
        logins: Iterable[str] = (),
        emails: Iterable[str] = (),
    ) -> "Suppression":
        """Build from raw values, normalizing case and stripping blanks."""
        return cls(
            logins=frozenset(v.strip().lower() for v in logins if v and v.strip()),
            emails=frozenset(v.strip().lower() for v in emails if v and v.strip()),
        )

    def __bool__(self) -> bool:
        return bool(self.logins or self.emails)


def apply_suppression(data: RepoData, suppression: Optional[Suppression]) -> int:
    """Strip suppressed people from collected data in place.

    Returns how many subjects were cleared — one per person or channel, not one
    per field — so the caller can log that an objection was honoured on this
    scan without naming anyone in the job log. The log is visible in the admin
    panel, and "who objected" is precisely the thing an objection is about.
    """
    if not suppression:
        return 0

    removed = 0

    if suppression.logins:
        # The owner is a separate subject in the jurisdiction metric, not a
        # contributor, and it is the one an organization account usually
        # appears as. Clearing the login itself would break the report (the
        # owner is how a repository is addressed), so only the personal fields
        # go — which are exactly the ones the metric reads.
        owner = data.owner
        if owner is not None and (owner.login or "").strip().lower() in suppression.logins:
            cleared = False
            for field in ("name", "company", "blog", "location"):
                if getattr(owner, field, None) is not None:
                    setattr(owner, field, None)
                    cleared = True
            removed += 1 if cleared else 0

        for contributor in data.maintainership.top_contributors:
            login = (contributor.login or "").strip().lower()
            if login and login in suppression.logins and contributor.profile is not None:
                contributor.profile = None
                removed += 1

    if suppression.emails and data.contacts:
        kept = [
            channel
            for channel in data.contacts
            if (channel.value or "").strip().lower() not in suppression.emails
        ]
        removed += len(data.contacts) - len(kept)
        data.contacts = kept

    return removed
