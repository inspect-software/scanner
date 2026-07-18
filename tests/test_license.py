from __future__ import annotations

import pytest

from scanner.license import normalize_spdx, resolve_license


def test_recognized_identifier_is_standard():
    info = resolve_license(raw_spdx="MIT", profile_has_license=True, scorecard_license_score=10)
    assert info.state == "standard"
    assert info.spdx_id == "MIT"


def test_noassertion_is_a_custom_license_not_a_missing_one():
    # The bug this module exists to prevent: GitHub says "there is a LICENSE
    # file, I just cannot classify its text", and we used to render that as
    # "No license detected" while scoring it as present.
    info = resolve_license(raw_spdx="NOASSERTION", profile_has_license=True, scorecard_license_score=9)
    assert info.state == "custom"
    assert info.spdx_id is None
    assert info.raw_spdx == "NOASSERTION"


def test_nothing_anywhere_is_absent():
    info = resolve_license(raw_spdx=None, profile_has_license=False, scorecard_license_score=0)
    assert info.state == "absent"
    assert info.file_present is False


@pytest.mark.parametrize(
    "profile, score",
    [
        (False, 9),  # Scorecard sees a file the community profile misses
        (True, 0),   # and the reverse
    ],
)
def test_presence_is_a_logical_or_across_sources(profile, score):
    # ~1% of repositories have the two GitHub sources disagreeing. A file one
    # source cannot see is still a file; telling a maintainer their license is
    # missing because one endpoint blinked is the worse error.
    info = resolve_license(raw_spdx=None, profile_has_license=profile, scorecard_license_score=score)
    assert info.state == "custom"
    assert info.file_present is True


def test_a_license_object_alone_counts_as_presence():
    # GitHub only emits a license object at all when it found something to
    # classify, so NOASSERTION implies a file even with no other source.
    info = resolve_license(raw_spdx="NOASSERTION", profile_has_license=False)
    assert info.state == "custom"


def test_missing_scorecard_is_not_a_negative_signal():
    info = resolve_license(raw_spdx=None, profile_has_license=True, scorecard_license_score=None)
    assert info.state == "custom"
    assert info.scorecard_found is None


def test_scorecard_participation_is_recorded_when_it_ran():
    assert resolve_license(
        raw_spdx=None, profile_has_license=False, scorecard_license_score=0
    ).scorecard_found is False


def test_normalize_spdx_drops_only_the_sentinel_and_blanks():
    assert normalize_spdx("Apache-2.0") == "Apache-2.0"
    assert normalize_spdx("NOASSERTION") is None
    assert normalize_spdx(None) is None
    assert normalize_spdx("") is None
