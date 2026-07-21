"""Release-tag classification and the release list carried in the report."""

from __future__ import annotations

import pytest

from scanner.collect import _release_kind, _release_records


@pytest.mark.parametrize(
    "tag,kind",
    [
        ("1.0.0", "major"),
        ("v2.0.0", "major"),
        ("v0.0.0", "major"),          # minor and patch both zero
        ("1.4.0", "minor"),
        ("v0.5.0", "minor"),          # 0.x line: a minor bump is still a minor
        ("1.4.7", "patch"),
        ("v0.0.3", "patch"),
        ("1.0.0-rc1", "prerelease"),
        ("v2.1.0-beta.2", "prerelease"),
        ("1.2.3+build.5", "patch"),   # build metadata is not a prerelease
        ("2.0.0+20260721", "major"),
        ("release-2026-07", "other"),
        ("nightly", "other"),
        ("1.2", "other"),             # not three-part
        ("", "other"),
        (None, "other"),
    ],
)
def test_release_kind_classifies_from_the_tag_alone(tag, kind):
    assert _release_kind(tag) == kind


def test_records_keep_order_and_drop_untagged_entries():
    records = _release_records([
        ("v2.0.0", "2026-01-01T00:00:00Z"),
        (None, "2025-01-01T00:00:00Z"),   # no tag -> dropped
        ("v1.9.1", "2025-06-01T00:00:00Z"),
    ])
    assert [r.tag for r in records] == ["v2.0.0", "v1.9.1"]
    assert [r.kind for r in records] == ["major", "patch"]


def test_missing_date_is_preserved_as_none():
    # The REST tag fallback resolves commit dates only for the cadence window,
    # so tags beyond it legitimately arrive without one.
    records = _release_records([("v1.0.0", None), ("v1.0.1", "")])
    assert [r.published_at for r in records] == [None, None]
    assert [r.kind for r in records] == ["major", "patch"]
