"""OpenSSF Scorecard parsing/mapping and its use in the security metric."""

import json

from scanner.metrics import metric_security_posture
from scanner.models import RepoData, Scorecard, ScorecardCheck, SecuritySignals
from scanner.render import render_html
from scanner.scorecard import (
    CHECK_RISK,
    RISK_WEIGHTS,
    check_weight,
    failure_detail,
    map_scorecard,
    parse_scorecard,
)

SAMPLE = {
    "date": "2026-07-06T00:00:00Z",
    "repo": {"name": "github.com/acme/widget", "commit": "deadbeef"},
    "scorecard": {"version": "v5.0.0"},
    "score": 6.3,
    "checks": [
        {"name": "Binary-Artifacts", "score": 10, "reason": "no binaries",
         "documentation": {"url": "https://x/bin"}},
        {"name": "Branch-Protection", "score": -1, "reason": "no admin token"},
        {"name": "Token-Permissions", "score": 0, "reason": "default broad perms"},
        {"name": "Vulnerabilities", "score": 10, "reason": "no known vulns"},
    ],
}


# --- pure parse / map ---------------------------------------------------------


def test_map_scorecard_basic():
    sc = map_scorecard(SAMPLE)
    assert sc.aggregate_score == 6.3
    assert sc.scorecard_version == "v5.0.0"
    assert sc.commit == "deadbeef"
    assert sc.ran_at is not None
    by = {c.name: c for c in sc.checks}
    assert by["Binary-Artifacts"].score == 10
    assert by["Binary-Artifacts"].documentation_url == "https://x/bin"
    # -1 becomes None (inconclusive), not a real 0
    assert by["Branch-Protection"].score is None


def test_map_scorecard_negative_aggregate_is_none():
    sc = map_scorecard({"score": -1, "checks": []})
    assert sc.aggregate_score is None


def test_parse_scorecard_from_json_string():
    sc = parse_scorecard(json.dumps(SAMPLE))
    assert sc is not None and sc.aggregate_score == 6.3


def test_parse_scorecard_tolerates_leading_log_line():
    noisy = "warning: something\n" + json.dumps(SAMPLE)
    sc = parse_scorecard(noisy)
    assert sc is not None and len(sc.checks) == 4


def test_parse_scorecard_rejects_garbage():
    assert parse_scorecard("") is None
    assert parse_scorecard("not json at all") is None
    assert parse_scorecard("[1, 2, 3]") is None  # not an object


def test_check_weight_uses_risk_table_and_defaults_medium():
    assert check_weight("Token-Permissions") == RISK_WEIGHTS[CHECK_RISK["Token-Permissions"]]
    assert check_weight("Dangerous-Workflow") == RISK_WEIGHTS["Critical"]
    # unknown / future check -> Medium
    assert check_weight("Some-Future-Check") == RISK_WEIGHTS["Medium"]


# --- security metric from Scorecard -------------------------------------------


def _data_with(checks):
    return RepoData(security_signals=SecuritySignals(
        scorecard=Scorecard(aggregate_score=None, checks=checks)))


def test_security_metric_uses_scorecard_when_present():
    data = RepoData(security_signals=SecuritySignals(scorecard=map_scorecard(SAMPLE)))
    m = metric_security_posture(data)
    assert m.inputs["source"] == "openssf_scorecard"
    names = {c.name for c in m.components}
    assert "Token-Permissions" in names and "Vulnerabilities" in names


def test_security_metric_excludes_inconclusive_checks_not_zero():
    # Two identical strong checks; one is inconclusive. The inconclusive one must
    # be excluded (renormalized away), so the score stays high, not halved.
    strong_only = _data_with([
        ScorecardCheck(name="Vulnerabilities", score=10),
    ])
    strong_plus_inconclusive = _data_with([
        ScorecardCheck(name="Vulnerabilities", score=10),
        ScorecardCheck(name="Branch-Protection", score=None),
    ])
    a = metric_security_posture(strong_only)
    b = metric_security_posture(strong_plus_inconclusive)
    assert a.value == b.value == 100
    excluded = [c for c in b.components if c.status == "excluded"]
    assert [c.name for c in excluded] == ["Branch-Protection"]
    assert "renormalized" in (b.note or "").lower()


def test_security_metric_tracks_scorecard_scale():
    # A perfect Scorecard -> 100; an all-zero Scorecard -> 1 (clamped).
    perfect = _data_with([ScorecardCheck(name="SAST", score=10),
                          ScorecardCheck(name="Token-Permissions", score=10)])
    worst = _data_with([ScorecardCheck(name="SAST", score=0),
                        ScorecardCheck(name="Token-Permissions", score=0)])
    assert metric_security_posture(perfect).value == 100
    assert metric_security_posture(worst).value == 1


def test_security_metric_risk_weighting():
    # A failed Critical check should hurt more than a failed Low check.
    fail_critical = _data_with([ScorecardCheck(name="Dangerous-Workflow", score=0),  # Critical
                               ScorecardCheck(name="CI-Tests", score=10)])            # Low
    fail_low = _data_with([ScorecardCheck(name="Dangerous-Workflow", score=10),
                          ScorecardCheck(name="CI-Tests", score=0)])
    assert metric_security_posture(fail_critical).value < metric_security_posture(fail_low).value


# --- fallback to file signals -------------------------------------------------


def test_security_metric_falls_back_to_file_signals_without_scorecard():
    data = RepoData(security_signals=SecuritySignals(
        has_security_policy=True, has_dependabot_config=True, has_codeql_workflow=True))
    m = metric_security_posture(data)
    assert m.inputs["source"] == "file_signals"
    assert {"Security policy (SECURITY.md)"} <= {c.name for c in m.components}


# --- rendering ----------------------------------------------------------------


def test_render_scorecard_section():
    from datetime import datetime, timezone

    from scanner.metrics import compute_metrics
    from scanner.models import RepoInfo, RepoRef, Report

    data = RepoData(
        repo=RepoInfo(primary_language="Python"),
        security_signals=SecuritySignals(scorecard=map_scorecard(SAMPLE)),
    )
    report = Report(
        generated_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        source=RepoRef(url="acme/widget", owner="acme", name="widget"),
        data=data,
        metrics=compute_metrics(data),
    )
    html = render_html(report)
    assert "<h2>OpenSSF Scorecard</h2>" in html  # the dedicated section
    assert "Token-Permissions" in html
    assert "n/a" in html  # the inconclusive Branch-Protection check
    assert "v5.0.0" in html


def test_render_no_scorecard_section_when_absent():
    from datetime import datetime, timezone

    from scanner.metrics import compute_metrics
    from scanner.models import RepoInfo, RepoRef, Report

    data = RepoData(repo=RepoInfo(primary_language="Python"))
    report = Report(
        generated_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        source=RepoRef(url="acme/widget", owner="acme", name="widget"),
        data=data,
        metrics=compute_metrics(data),
    )
    assert "<h2>OpenSSF Scorecard</h2>" not in render_html(report)


# --- failure diagnostics ---------------------------------------------------

# Scorecard's real sign-off on production runs: an advisory, printed after
# whatever went wrong, and the last line of stderr.
PAT_WARNING = "2026/08/22 11:37:31 Warning: PATs stored in the environment"


def test_failure_detail_names_a_signal_kill_rather_than_a_trailing_warning():
    # The kill that was filed as "Warning: PATs stored...": the process died
    # with SIGKILL and the advisory was simply the last thing it had printed.
    detail = failure_detail(-9, PAT_WARNING + "\n")
    assert "killed by SIGKILL" in detail
    assert "memory limit" in detail


def test_failure_detail_reports_a_kill_that_printed_nothing():
    assert failure_detail(-9, "") == (
        "killed by SIGKILL — most likely the container's memory limit"
    )


def test_failure_detail_prefers_the_error_line_over_a_later_advisory():
    stderr = "\n".join([
        "2026/08/22 11:37:29 error: failed to clone github.com/acme/widget",
        PAT_WARNING,
    ])
    detail = failure_detail(1, stderr)
    assert "exit code 1" in detail
    assert "failed to clone" in detail
    assert "PATs stored" not in detail


def test_failure_detail_falls_back_to_the_last_non_advisory_line():
    stderr = "\n".join(["2026/08/22 11:37:29 repo unreachable", PAT_WARNING])
    assert failure_detail(2, stderr).endswith("repo unreachable")


def test_failure_detail_keeps_an_advisory_when_it_is_all_there_is():
    assert failure_detail(1, PAT_WARNING).endswith("PATs stored in the environment")


def test_failure_detail_always_carries_the_exit_status():
    assert failure_detail(3, "").startswith("exit code 3")
