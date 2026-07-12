"""OpenSSF Scorecard integration via the open-source ``scorecard`` CLI.

Scorecard (https://github.com/ossf/scorecard) is a neutral, versioned,
tool-agnostic security-scoring standard: each check rewards a *practice*
(e.g. "uses some dependency-update tool", "runs some SAST") rather than a
specific vendor's config file, and unknown/undeterminable checks return ``-1``
(inconclusive) instead of a misleading zero. That maps cleanly onto this
scanner's own "missing data is excluded and renormalized" rule, which is why
we lean on Scorecard as the backbone of the security metric instead of
detecting individual vendor files.

The module keeps the pure halves (``parse_scorecard`` / ``map_scorecard``)
separate from the network/subprocess half (``run_scorecard``) so scoring and
rendering can be unit-tested against sample output without the binary. Running
Scorecard is best-effort: a missing binary, a timeout, or a failure degrades
to ``None`` plus a warning and never aborts a scan.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .models import Scorecard, ScorecardCheck

DEFAULT_BINARY = "scorecard"
DEFAULT_TIMEOUT = 240.0  # Scorecard clones + hits the GitHub API; it is not fast.

# Scorecard groups its checks by risk level; its aggregate score is a
# risk-weighted mean of the checks that ran. We reuse the same weights so our
# derived component breakdown tracks Scorecard's own methodology. Weights per
# the Scorecard docs (Critical 10 / High 7.5 / Medium 5 / Low 2.5).
RISK_WEIGHTS: dict[str, float] = {
    "Critical": 10.0,
    "High": 7.5,
    "Medium": 5.0,
    "Low": 2.5,
}

# Check -> risk level (from ossf/scorecard docs/checks). Unknown/newly added
# checks fall back to Medium so they are still scored, not silently dropped.
CHECK_RISK: dict[str, str] = {
    "Binary-Artifacts": "High",
    "Branch-Protection": "High",
    "CI-Tests": "Low",
    "CII-Best-Practices": "Low",
    "Code-Review": "High",
    "Contributors": "Low",
    "Dangerous-Workflow": "Critical",
    "Dependency-Update-Tool": "High",
    "Fuzzing": "Medium",
    "License": "Low",
    "Maintained": "High",
    "Packaging": "Medium",
    "Pinned-Dependencies": "Medium",
    "SAST": "Medium",
    "Security-Policy": "Medium",
    "Signed-Releases": "High",
    "Token-Permissions": "High",
    "Vulnerabilities": "High",
    "Webhooks": "Critical",
}

_DEFAULT_RISK = "Medium"


def check_weight(name: str) -> float:
    """Risk weight Scorecard assigns to a check (Medium for unknown checks)."""
    return RISK_WEIGHTS[CHECK_RISK.get(name, _DEFAULT_RISK)]


# ---------------------------------------------------------------------------
# Pure parsing / mapping
# ---------------------------------------------------------------------------


def _iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def map_scorecard(payload: dict[str, Any]) -> Scorecard:
    """Turn a parsed ``scorecard --format=json`` payload into a Scorecard.

    A check or aggregate score of ``-1`` (Scorecard's "inconclusive") becomes
    ``None`` so downstream scoring excludes it rather than scoring it zero."""
    checks: list[ScorecardCheck] = []
    for c in payload.get("checks") or []:
        raw = c.get("score")
        score = raw if isinstance(raw, int) and 0 <= raw <= 10 else None
        checks.append(
            ScorecardCheck(
                name=c.get("name", "?"),
                score=score,
                reason=c.get("reason"),
                documentation_url=(c.get("documentation") or {}).get("url"),
            )
        )
    agg = payload.get("score")
    aggregate = float(agg) if isinstance(agg, (int, float)) and agg >= 0 else None
    return Scorecard(
        aggregate_score=aggregate,
        checks=checks,
        scorecard_version=(payload.get("scorecard") or {}).get("version"),
        ran_at=_iso(payload.get("date")),
        commit=(payload.get("repo") or {}).get("commit"),
    )


def parse_scorecard(stdout: str) -> Optional[Scorecard]:
    """Parse the CLI's JSON stdout; None if it is not usable JSON."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        # Be forgiving if a stray line preceded the JSON object.
        start = text.find("{")
        if start <= 0:
            return None
        try:
            payload = json.loads(text[start:])
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    return map_scorecard(payload)


# ---------------------------------------------------------------------------
# Best-effort CLI invocation
# ---------------------------------------------------------------------------


def scorecard_available(binary: str = DEFAULT_BINARY) -> bool:
    return shutil.which(binary) is not None


def run_scorecard(
    owner: str,
    repo: str,
    token: Optional[str],
    warnings: list[str],
    *,
    binary: str = DEFAULT_BINARY,
    timeout: float = DEFAULT_TIMEOUT,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[Scorecard]:
    """Run the Scorecard CLI against a public repo; best-effort.

    Returns None (with a warning) when the binary is missing, times out, or
    fails to produce a usable result — a Scorecard failure never aborts a scan.
    ``log``, if given, receives a one-line progress message before and after
    the subprocess call (the CLI itself only emits its JSON payload, so there
    is nothing meaningful to stream mid-run).
    """
    emit = log or (lambda _msg: None)
    exe = shutil.which(binary)
    if not exe:
        warnings.append(
            "OpenSSF Scorecard CLI not found on PATH; security scoring falls back "
            "to basic file checks (install github.com/ossf/scorecard for full "
            "tool-agnostic scoring)"
        )
        emit("OpenSSF Scorecard CLI not found on PATH; skipping.")
        return None

    env = dict(os.environ)
    if token:
        # Scorecard reads the token from GITHUB_AUTH_TOKEN (GITHUB_TOKEN also works).
        env["GITHUB_AUTH_TOKEN"] = token

    cmd = [exe, f"--repo=github.com/{owner}/{repo}", "--format=json"]
    emit(f"Running OpenSSF Scorecard against {owner}/{repo}…")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        warnings.append(
            f"OpenSSF Scorecard timed out after {timeout:g}s; skipping Scorecard checks"
        )
        emit(f"OpenSSF Scorecard timed out after {timeout:g}s; skipping.")
        return None
    except OSError as exc:
        warnings.append(f"Could not run OpenSSF Scorecard: {exc}")
        emit(f"Could not run OpenSSF Scorecard: {exc}")
        return None

    result = parse_scorecard(proc.stdout)
    if result is None:
        stderr = (proc.stderr or "").strip()
        detail = stderr.splitlines()[-1] if stderr else f"exit code {proc.returncode}"
        warnings.append(
            f"OpenSSF Scorecard did not return a usable result ({detail}); "
            "skipping Scorecard checks"
        )
        emit(f"OpenSSF Scorecard did not return a usable result ({detail}).")
        return None
    emit(f"OpenSSF Scorecard aggregate score: {result.aggregate_score}")
    return result
