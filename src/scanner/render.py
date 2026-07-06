"""Render a Report into a single-file, human-readable HTML page.

The page is self-contained except for CDN assets (Inter font, Chart.js for
the score radar, Lucide icons); it degrades gracefully when CDNs are
unreachable. Metric explanations here mirror docs/metrics.md — keep them in
sync when the methodology changes.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, Optional, Union

from jinja2 import Environment, FileSystemLoader

from .metrics import ORG_OVERALL_WEIGHTS, OVERALL_WEIGHTS
from .models import Metric, OrgReport, Report

BAND_META: dict[str, dict[str, str]] = {
    "excellent": {
        "label": "Excellent",
        "color": "#10b981",
        "range": "85–100",
        "meaning": "Exemplary; meets essentially all checked criteria",
    },
    "good": {
        "label": "Good",
        "color": "#84cc16",
        "range": "70–84",
        "meaning": "Healthy; minor gaps",
    },
    "moderate": {
        "label": "Moderate",
        "color": "#f59e0b",
        "range": "50–69",
        "meaning": "Acceptable with notable gaps; review recommended",
    },
    "at_risk": {
        "label": "At risk",
        "color": "#f97316",
        "range": "30–49",
        "meaning": "Significant weaknesses; adoption warrants caution",
    },
    "critical": {
        "label": "Critical",
        "color": "#ef4444",
        "range": "1–29",
        "meaning": "Severe problems (abandoned, single-maintainer, no hygiene)",
    },
}

# Display order, icons and explanations for each metric (mirrors docs/metrics.md).
METRIC_INFO: dict[str, dict[str, Any]] = {
    "activity": {
        "name": "Development activity",
        "icon": "activity",
        "question": "Is the project actively developed?",
        "explanation": (
            "Measures whether the project is alive: how recently code was pushed, "
            "how consistently commits land week over week, overall commit volume "
            "over the last year, and whether releases ship on a regular cadence."
        ),
        "components": [
            ("Push recency", 35, "≤7 days since last push scores full points; >1 year scores none"),
            ("Commit cadence", 35, "share of the last 52 weeks with at least one commit"),
            ("Commit volume", 15, "log-scale; ~100 commits/year saturates"),
            ("Release practice", 15, "mean gap ≤45 days scores full points"),
        ],
    },
    "maintainer_resilience": {
        "name": "Maintainer resilience",
        "icon": "users",
        "question": "Can the project survive losing its top maintainer?",
        "explanation": (
            "The classic bus-factor risk: how many people the project actually "
            "depends on. A single dominant maintainer is the most common failure "
            "mode of open-source projects — one person burning out, changing jobs, "
            "or walking away can end the project."
        ),
        "components": [
            ("Bus factor", 60, "contributors needed to cover 50% of commits; 1 scores very low, 5+ scores high"),
            ("Commit distribution", 25, "the smaller the top contributor's share of commits, the better"),
            ("Contributor breadth", 15, "total contributors; 10+ saturates"),
        ],
    },
    "responsiveness": {
        "name": "Issue & PR responsiveness",
        "icon": "message-square",
        "question": "Are issues and pull requests actually being handled?",
        "explanation": (
            "Whether the maintainers engage with what the community brings them: "
            "the lifetime share of issues that get closed, and the share of decided "
            "pull requests that get merged rather than rejected or ignored."
        ),
        "components": [
            ("Issue resolution", 55, "lifetime closed / (open + closed) issue ratio"),
            ("PR acceptance", 45, "merged / (merged + closed-unmerged) pull requests"),
        ],
    },
    "community_health": {
        "name": "Community health",
        "icon": "heart-handshake",
        "question": "Is the project set up to receive users and contributors?",
        "explanation": (
            "Onboarding readiness: the documents and templates that tell a new "
            "user or contributor what this project is, how to use it legally, and "
            "how to participate — README, license, contribution guide, code of "
            "conduct, issue/PR templates and a documentation directory."
        ),
        "components": [
            ("README", 25, ""),
            ("License", 20, ""),
            ("CONTRIBUTING guide", 15, ""),
            ("Code of conduct", 10, ""),
            ("Issue template", 10, ""),
            ("Docs directory", 10, ""),
            ("PR template", 5, ""),
            ("Repo description", 5, ""),
        ],
    },
    "engineering_practices": {
        "name": "Engineering practices",
        "icon": "wrench",
        "question": "Does the project follow baseline engineering hygiene?",
        "explanation": (
            "Publicly visible quality practices: continuous integration, a test "
            "suite, linter configuration, pre-commit hooks and editor conventions. "
            "Presence signals — they show the practices exist, not how good they are."
        ),
        "components": [
            ("CI workflows", 30, ""),
            ("Tests present", 30, ""),
            ("Linter config", 15, ""),
            ("Pre-commit hooks", 10, ""),
            ("Docs directory", 10, ""),
            (".editorconfig", 5, ""),
        ],
    },
    "security_posture": {
        "name": "Security posture",
        "icon": "shield-check",
        "question": "Does the project practice visible security hygiene?",
        "explanation": (
            "Supply-chain and vulnerability-handling signals: a security policy "
            "telling researchers how to report vulnerabilities, automated "
            "dependency updates, static security scanning, and lockfiles that pin "
            "dependencies. This is not a security audit of the code itself."
        ),
        "components": [
            ("Security policy (SECURITY.md)", 30, ""),
            ("Dependabot config", 25, ""),
            ("Dependency lockfiles", 25, "only scored when the repo declares dependencies"),
            ("CodeQL workflow", 20, ""),
        ],
    },
}

# Explanations for organization metrics (mirrors docs/metrics.md).
ORG_METRIC_INFO: dict[str, dict[str, Any]] = {
    "portfolio_activity": {
        "name": "Portfolio activity",
        "icon": "folder-git-2",
        "question": "Is the organization's repository portfolio actively maintained?",
        "explanation": (
            "Whether the organization's public repositories are alive as a whole: "
            "the share recently pushed, the share touched within a year, the size "
            "of the portfolio, and how much of it is original work rather than forks. "
            "Computed over a sample of up to 100 most recently pushed public repos."
        ),
        "components": [
            ("Recently active repos", 50, "share of sampled repos pushed in the last 90 days"),
            ("Yearly active repos", 25, "share of sampled repos pushed in the last year"),
            ("Portfolio size", 15, "log-scale; ~100 public repos saturates"),
            ("Original work", 10, "share of sampled repos that are not forks"),
        ],
    },
    "community_reach": {
        "name": "Community reach",
        "icon": "megaphone",
        "question": "Does the organization have community traction?",
        "explanation": (
            "Public traction signals: followers of the organization account and "
            "stars accumulated across its repositories. Both are log-scaled — "
            "going from 10 to 100 matters more than from 10,000 to 10,100."
        ),
        "components": [
            ("Followers", 50, "log-scale; ~1,000 followers saturates"),
            ("Stars across repositories", 50, "log-scale over sampled repos; ~10,000 stars saturates"),
        ],
    },
    "profile_completeness": {
        "name": "Profile completeness",
        "icon": "building-2",
        "question": "Is the organization profile complete and accountable?",
        "explanation": (
            "A filled-in, verifiable profile signals an accountable organization "
            "behind the code: a GitHub-verified domain, a description, homepage, "
            "location and contact channels."
        ),
        "components": [
            ("Verified domain", 25, "GitHub's verified-domain badge"),
            ("Description", 20, ""),
            ("Homepage", 15, ""),
            ("Display name", 10, ""),
            ("Location", 10, ""),
            ("Contact email", 10, ""),
            ("Social profile", 10, "Twitter/X handle on the profile"),
        ],
    },
}

_env = Environment(
    loader=FileSystemLoader(str(files("scanner") / "templates")),
    autoescape=True,
)


def _fmt_input(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "none"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


STATUS_META: dict[str, dict[str, str]] = {
    "met": {"icon": "circle-check", "color": "#10b981"},
    "partial": {"icon": "circle-dot", "color": "#f59e0b"},
    "missed": {"icon": "circle-x", "color": "#ef4444"},
    "excluded": {"icon": "circle-minus", "color": "#94a3b8"},
}


def _fmt_pts(value: float) -> str:
    return f"{value:g}"


def _component_rows(metric: Metric, info: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge computed components with the static rule hints from the info map."""
    hints = {name: hint for name, _, hint in info["components"]}
    rows = []
    for c in metric.components:
        rows.append(
            {
                "name": c.name,
                "pts": f"{_fmt_pts(c.points)}/{_fmt_pts(c.max_points)}",
                "status": c.status,
                "icon": STATUS_META[c.status]["icon"],
                "color": STATUS_META[c.status]["color"],
                "detail": c.detail,
                "hint": hints.get(c.name, ""),
            }
        )
    return rows


def _metric_view(
    key: str,
    metric: Optional[Metric],
    info_map: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> dict[str, Any]:
    info = info_map[key]
    view: dict[str, Any] = {
        "key": key,
        "name": info["name"],
        "icon": info["icon"],
        "question": info["question"],
        "explanation": info["explanation"],
        "static_components": info["components"],
        "weight": weights.get(key),
        "missing": metric is None,
        "component_rows": [],
    }
    if metric is not None:
        band = BAND_META[metric.band]
        view.update(
            value=metric.value,
            band=metric.band,
            band_label=band["label"],
            color=band["color"],
            note=metric.note,
            inputs=[(k.replace("_", " "), _fmt_input(v)) for k, v in metric.inputs.items()],
            component_rows=_component_rows(metric, info),
        )
    return view


def _shared_context(
    report: Union[Report, OrgReport], metric_views: list[dict[str, Any]]
) -> dict[str, Any]:
    metrics = report.metrics
    overall = metrics.overall if metrics else None
    overall_band = BAND_META[overall.band] if overall else None
    scored = [v for v in metric_views if not v["missing"]]
    chart_payload = {
        "labels": [v["name"] for v in scored],
        "values": [v["value"] for v in scored],
        "color": overall_band["color"] if overall_band else "#64748b",
    }
    return {
        "report": report,
        "source": report.source,
        "generated": report.generated_at.strftime("%Y-%m-%d %H:%M UTC"),
        "overall": overall,
        "overall_band": overall_band,
        "accent": overall_band["color"] if overall_band else "#64748b",
        "metric_views": metric_views,
        "bands": [BAND_META[k] for k in ("excellent", "good", "moderate", "at_risk", "critical")],
        "chart_json": json.dumps(chart_payload).replace("</", "<\\/"),
        "report_json": report.model_dump_json(indent=2).replace("</", "<\\/"),
        "warnings": report.warnings,
        "metrics_version": metrics.metrics_version if metrics else None,
    }


def render_html(report: Report) -> str:
    metrics = report.metrics
    metric_views = [
        _metric_view(key, getattr(metrics, key) if metrics else None, METRIC_INFO, OVERALL_WEIGHTS)
        for key in METRIC_INFO
    ]
    context = _shared_context(report, metric_views)
    context.update(
        data=report.data,
        repo_url=f"https://github.com/{report.source.owner}/{report.source.name}",
    )
    return _env.get_template("report.html.j2").render(**context)


def render_org_html(report: OrgReport) -> str:
    metrics = report.metrics
    metric_views = [
        _metric_view(
            key, getattr(metrics, key) if metrics else None, ORG_METRIC_INFO, ORG_OVERALL_WEIGHTS
        )
        for key in ORG_METRIC_INFO
    ]
    context = _shared_context(report, metric_views)
    context.update(
        info=report.data.info,
        portfolio=report.data.portfolio,
        org_url=f"https://github.com/{report.source.login}",
    )
    return _env.get_template("org.html.j2").render(**context)
