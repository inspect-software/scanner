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

from .ecosystems import ranked_ecosystems
from .metrics import ORG_CATEGORIES, REPO_CATEGORIES
from .models import Metric, MetricCategory, OrgReport, Report

BAND_META: dict[str, dict[str, str]] = {
    "excellent": {"label": "Excellent", "color": "#10b981", "range": "85–100",
                  "meaning": "Exemplary; meets essentially all checked criteria"},
    "good": {"label": "Good", "color": "#84cc16", "range": "70–84",
             "meaning": "Healthy; minor gaps"},
    "moderate": {"label": "Moderate", "color": "#f59e0b", "range": "50–69",
                 "meaning": "Acceptable with notable gaps; review recommended"},
    "at_risk": {"label": "At risk", "color": "#f97316", "range": "30–49",
                "meaning": "Significant weaknesses; adoption warrants caution"},
    "critical": {"label": "Critical", "color": "#ef4444", "range": "1–29",
                 "meaning": "Severe problems (abandoned, single-maintainer, no hygiene)"},
}

# Category display icons, keyed by category key.
CATEGORY_ICONS: dict[str, str] = {
    "vitality": "heart-pulse",
    "community": "users-round",
    "governance": "landmark",
    "engineering": "wrench",
    "security": "shield-check",
    "ai_readiness": "bot",
    "activity_reach": "trending-up",
}

# Icons, questions and explanations per metric (mirrors docs/metrics.md). The
# component tuples (name, weight, hint) must use the SAME names metrics.py emits.
METRIC_INFO: dict[str, dict[str, Any]] = {
    "development_activity": {
        "icon": "activity",
        "question": "Is code actively being written?",
        "explanation": (
            "How recently code was pushed, how consistently commits land week over "
            "week, and the overall commit volume over the last year."
        ),
        "components": [
            ("Push recency", 40, "≤7 days since last push scores full points; >1 year scores none"),
            ("Commit cadence", 40, "share of the last 52 weeks with at least one commit"),
            ("Commit volume", 20, "log-scale; ~100 commits/year saturates"),
        ],
    },
    "release_discipline": {
        "icon": "tag",
        "question": "Does the project ship versioned releases?",
        "explanation": (
            "Whether the project publishes releases at all, how recently the latest "
            "one shipped, and how regular the release cadence is. Libraries that cut "
            "releases give downstream users something stable to pin to."
        ),
        "components": [
            ("Ships releases", 30, "any published releases"),
            ("Release recency", 40, "latest release ≤90 days ago scores full points"),
            ("Release cadence", 30, "mean gap ≤45 days scores full points"),
        ],
    },
    "popularity": {
        "icon": "star",
        "question": "How much adoption and attention does it have?",
        "explanation": (
            "Stars, forks and watchers as adoption signals, all log-scaled so going "
            "from 10 to 100 counts far more than 10,000 to 10,100."
        ),
        "components": [
            ("Stars", 60, "log-scale; ~5,000 stars saturates"),
            ("Forks", 25, "log-scale; ~1,000 forks saturates"),
            ("Watchers", 15, "log-scale; ~500 watchers saturates"),
        ],
    },
    "ecosystem_adoption": {
        "icon": "download",
        "question": "How widely is the package actually installed?",
        "explanation": (
            "Real download counts from the package registry (PyPI, npm, Packagist, "
            "crates.io) — a far stronger adoption signal than GitHub stars, since "
            "people install libraries they never star. Log-scaled. Only scored for "
            "repos that publish a package."
        ),
        "components": [
            ("Monthly downloads", 80, "log-scale; ~1,000,000 downloads/month saturates"),
            ("Total downloads", 80, "fallback for registries with no monthly figure (e.g. RubyGems); ~50,000,000 all-time saturates"),
            ("Registry dependents", 20, "packages depending on it; reported by some ecosystems only"),
        ],
    },
    "maintainer_resilience": {
        "icon": "users",
        "question": "Can it survive losing its top maintainer?",
        "explanation": (
            "The classic bus-factor risk: how many people the project depends on. A "
            "single dominant maintainer is the most common failure mode of open source."
        ),
        "components": [
            ("Bus factor", 60, "contributors needed to cover 50% of commits; 1 scores very low, 5+ high"),
            ("Commit distribution", 25, "the smaller the top contributor's share, the better"),
            ("Contributor breadth", 15, "total contributors; 10+ saturates"),
        ],
    },
    "responsiveness": {
        "icon": "message-square",
        "question": "Are issues and PRs actually being handled?",
        "explanation": (
            "The lifetime share of issues that get closed, and the share of decided "
            "pull requests that get merged rather than rejected or left to rot."
        ),
        "components": [
            ("Issue resolution", 55, "lifetime closed / (open + closed) issue ratio"),
            ("PR acceptance", 45, "merged / (merged + closed-unmerged) pull requests"),
        ],
    },
    "stewardship": {
        "icon": "landmark",
        "question": "Who stands behind this repository?",
        "explanation": (
            "Whether the repo is backed by an organization or a single personal "
            "account, whether that organization has a GitHub-verified domain, the "
            "owner's reach (followers), and how established the account is. "
            "Organization backing signals shared, accountable stewardship that can "
            "outlive any one person — so it lifts the score."
        ),
        "components": [
            ("Ownership backing", 30, "organization-owned scores far higher than a personal account"),
            ("Verified domain", 20, "GitHub verified-domain badge; not applicable to user accounts"),
            ("Owner reach", 25, "followers of the owning account, log-scaled"),
            ("Track record", 25, "account age and number of public repositories"),
        ],
    },
    "package_maintenance": {
        "icon": "package-check",
        "question": "Is the published package current and not deprecated?",
        "explanation": (
            "Registry-side upkeep, distinct from GitHub activity: how recently the "
            "package was published, whether it has a version history, and whether it "
            "is flagged deprecated (npm) or abandoned (Packagist) or has a yanked "
            "latest release. Only scored for repos that publish a package."
        ),
        "components": [
            ("Published & resolvable", 25, "the package resolves on its registry"),
            ("Publish recency", 35, "latest publish ≤180 days ago scores full points"),
            ("Version history", 20, "5+ published versions scores full points"),
            ("Not deprecated", 20, "deprecated / abandoned / yanked-latest scores zero"),
        ],
    },
    "community_health": {
        "icon": "heart-handshake",
        "question": "Is it set up to receive users and contributors?",
        "explanation": (
            "The documents and templates that welcome newcomers: README, license, "
            "contribution guide, code of conduct, and issue/PR templates."
        ),
        "components": [
            ("README", 25, ""),
            ("License", 25, ""),
            ("CONTRIBUTING guide", 20, ""),
            ("Code of conduct", 15, ""),
            ("Issue template", 8, ""),
            ("PR template", 7, ""),
        ],
    },
    "engineering_practices": {
        "icon": "wrench",
        "question": "Are baseline engineering practices in place?",
        "explanation": (
            "Publicly visible quality practices: continuous integration, a test "
            "suite, linter configuration, pre-commit hooks and editor conventions. "
            "Presence signals — they show the practice exists, not how good it is."
        ),
        "components": [
            ("CI workflows", 30, ""),
            ("Tests present", 30, ""),
            ("Linter config", 20, ""),
            ("Pre-commit hooks", 12, ""),
            (".editorconfig", 8, ""),
        ],
    },
    "documentation": {
        "icon": "book-open",
        "question": "Can a newcomer learn what it is and how to use it?",
        "explanation": (
            "Beyond a README: a dedicated docs directory, a documentation or project "
            "homepage, a repository description, discovery topics, and a wiki."
        ),
        "components": [
            ("README", 30, ""),
            ("Documentation directory", 25, "a docs/ directory in the tree"),
            ("Documentation / homepage site", 15, "the repo's homepage URL"),
            ("Repository description", 10, ""),
            ("Topics", 10, "GitHub topics aid discovery"),
            ("Wiki", 10, ""),
        ],
    },
    "security_posture": {
        "icon": "shield-check",
        "question": "Does it practice visible security hygiene?",
        "explanation": (
            "Primarily OpenSSF Scorecard — a neutral, tool-agnostic standard whose "
            "checks reward a practice (any accepted dependency-update tool, any SAST, "
            "signed releases, least-privilege workflow tokens, no known vulnerable "
            "dependencies…) rather than one vendor's config file. Each check is "
            "weighted by its risk level; checks Scorecard cannot determine are "
            "excluded, not scored zero. When the Scorecard CLI is unavailable, the "
            "score falls back to coarse file checks. Not a code audit."
        ),
        "components": [
            # Hints for the file-signal fallback; Scorecard checks show their own reason.
            ("Security policy (SECURITY.md)", 30, "fallback signal"),
            ("Dependabot config", 25, "fallback signal"),
            ("Dependency lockfiles", 25, "fallback: only scored when the repo declares dependencies"),
            ("CodeQL workflow", 20, "fallback signal"),
        ],
    },
    "high_risk_jurisdiction_exposure": {
        "icon": "flag-triangle-right",
        "question": "Does public profile evidence trigger the geopolitical supply-chain policy?",
        "explanation": (
            "Screens self-published owner, top-contributor, and public organization-profile "
            "locations for high-confidence Russia, Iran, or North Korea evidence. This is a "
            "supply-chain review signal, not an inference of nationality or intent. Ambiguous "
            "matches never affect the score."
        ),
        "components": [
            ("Policy exposure multiplier", 100,
             "Security multiplier: owner 20%, top contributor 50%, public organization affiliation 75%"),
        ],
    },
    # --- AI readiness metrics ---
    "ai_agent_context": {
        "icon": "file-code-2",
        "question": "Does it give AI agents guidance and machine-readable docs?",
        "explanation": (
            "Whether the repo ships agent-instruction files (CLAUDE.md, AGENTS.md, "
            "editor rules, Copilot instructions) and an llms.txt machine-readable "
            "docs entrypoint. A tiny stub instruction file scores partial, not full."
        ),
        "components": [
            ("Agent instructions", 60, "CLAUDE.md / AGENTS.md / editor rules; stub files score partial"),
            ("Machine-readable docs (llms.txt)", 40, "llms.txt / llms-full.txt present"),
        ],
    },
    "ai_verify_loop": {
        "icon": "circle-play",
        "question": "Can an agent set up, run, and verify a change on its own?",
        "explanation": (
            "The crux for autonomous agents — hence the heaviest weight here: a "
            "one-command bootstrap, an automated test suite to self-check against, "
            "lint/format and static type checking for fast feedback, and a "
            "reproducible environment (devcontainer / Dockerfile / Nix / lockfile)."
        ),
        "components": [
            ("One-command bootstrap", 25, "Makefile / Taskfile / justfile / mise / noxfile"),
            ("Automated tests", 30, "a test suite the agent can run to verify changes"),
            ("Lint / format config", 15, ""),
            ("Static type checking", 15, "typed language or a type-check config"),
            ("Reproducible environment", 15, "devcontainer / Dockerfile / Nix / lockfile"),
        ],
    },
    "ai_code_legibility": {
        "icon": "scan-text",
        "question": "Is the code legible to a model?",
        "explanation": (
            "Whether the code is statically type-checkable and free of oversized "
            "files that blow past an agent's working context. Only scored for repos "
            "with detectable source files."
        ),
        "components": [
            ("Type-checkable code", 45, "statically typed language, or a type-check config"),
            ("Manageable file sizes", 55, "share of source files under ~60KB (~1,500 lines)"),
        ],
    },
    "ai_interfaces": {
        "icon": "plug",
        "question": "Does it expose machine-readable interfaces?",
        "explanation": (
            "Machine-readable API schemas (OpenAPI / GraphQL / protobuf), a Model "
            "Context Protocol server, and runnable examples. Only scored when the "
            "repo exposes at least one — a plain library with none is not penalized."
        ),
        "components": [
            ("API schema (OpenAPI/GraphQL/proto)", 40, ""),
            ("MCP server", 20, "a Model Context Protocol server dependency or config"),
            ("Runnable examples", 40, "examples/ recipes/ or notebooks"),
        ],
    },
    # --- organization metrics ---
    "portfolio_activity": {
        "icon": "folder-git-2",
        "question": "Is the repository portfolio actively maintained?",
        "explanation": (
            "Whether the org's public repos are alive as a whole: the share recently "
            "pushed, the share touched within a year, portfolio size, and how much is "
            "original work. Over a sample of up to 100 most recently pushed repos."
        ),
        "components": [
            ("Recently active repos", 50, "share of sampled repos pushed in the last 90 days"),
            ("Yearly active repos", 25, "share of sampled repos pushed in the last year"),
            ("Portfolio size", 15, "log-scale; ~100 public repos saturates"),
            ("Original work", 10, "share of sampled repos that are not forks"),
        ],
    },
    "community_reach": {
        "icon": "megaphone",
        "question": "Does the organization have community traction?",
        "explanation": (
            "Followers of the organization account and stars accumulated across its "
            "repositories, both log-scaled."
        ),
        "components": [
            ("Followers", 50, "log-scale; ~1,000 followers saturates"),
            ("Stars across repositories", 50, "log-scale over sampled repos; ~10,000 stars saturates"),
        ],
    },
    "profile_completeness": {
        "icon": "building-2",
        "question": "Is the organization accountable and clearly presented?",
        "explanation": (
            "A filled-in, verifiable profile signals an accountable organization: a "
            "GitHub-verified domain, description, homepage, location and contacts."
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

# Human-readable metric names come from the computed Metric.name; but we also
# need a name before computing (unused now). Effective weight of each metric in
# the overall score = category weight × within-category weight.
def _effective_weights(specs) -> dict[str, float]:
    out: dict[str, float] = {}
    for spec in specs:
        if getattr(spec, "rollup", "weighted_mean") == "adjusted_security":
            # The adjusted posture is the 16% Security value; exposure is a
            # policy modifier, not an additive 8% metric.
            if "security_posture" in spec.metrics:
                out["security_posture"] = spec.weight
            continue
        for key, (within, _) in spec.metrics.items():
            out[key] = spec.weight * within
    return out


REPO_METRIC_WEIGHTS = _effective_weights(REPO_CATEGORIES)
ORG_METRIC_WEIGHTS = _effective_weights(ORG_CATEGORIES)

# Display names for the scan-configuration summary. Category names come from the
# specs; metric names are prettified keys (disabled metrics are never computed,
# so a computed Metric.name isn't available for them).
_CATEGORY_NAMES: dict[str, str] = {
    c.key: c.name for c in (*REPO_CATEGORIES, *ORG_CATEGORIES)
}


def _pretty_metric_name(key: str) -> str:
    return key.replace("_", " ").capitalize()

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
    hints = {name: hint for name, _, hint in info.get("components", [])}
    return [
        {
            "name": c.name,
            "pts": f"{_fmt_pts(c.points)}/{_fmt_pts(c.max_points)}",
            "status": c.status,
            "icon": STATUS_META[c.status]["icon"],
            "color": STATUS_META[c.status]["color"],
            "detail": c.detail,
            "hint": hints.get(c.name, ""),
        }
        for c in metric.components
    ]


def _metric_view(metric: Metric, weights: dict[str, float]) -> dict[str, Any]:
    info = METRIC_INFO.get(metric.key, {})
    band = BAND_META[metric.band]
    weight = weights.get(metric.key)
    return {
        "key": metric.key,
        "name": metric.name,
        "icon": info.get("icon", "gauge"),
        "question": info.get("question", ""),
        "explanation": info.get("explanation", ""),
        "static_components": info.get("components", []),
        "weight_pct": round(weight * 100) if weight else None,
        "value": metric.value,
        "band": metric.band,
        "band_label": band["label"],
        "color": band["color"],
        "note": metric.note,
        "inputs": [(k.replace("_", " "), _fmt_input(v)) for k, v in metric.inputs.items()],
        "component_rows": _component_rows(metric, info),
    }


def _category_views(
    categories: list[MetricCategory], weights: dict[str, float]
) -> list[dict[str, Any]]:
    views = []
    for cat in categories:
        band = BAND_META[cat.band] if cat.band else None
        views.append(
            {
                "key": cat.key,
                "name": cat.name,
                "description": cat.description,
                "icon": CATEGORY_ICONS.get(cat.key, "gauge"),
                "weight_pct": round(cat.weight * 100),
                "value": cat.value,
                "band_label": band["label"] if band else None,
                "color": band["color"] if band else "#94a3b8",
                "metrics": [_metric_view(m, weights) for m in cat.metrics],
            }
        )
    return views


def _config_view(config) -> dict[str, Any]:
    """Human-readable summary of the scan configuration for the report."""
    return {
        "is_default": config.is_default,
        "categories": [
            {"key": k, "name": _CATEGORY_NAMES.get(k, k)}
            for k in config.disabled_categories
        ],
        "metrics": [
            {"key": k, "name": _pretty_metric_name(k)} for k in config.disabled_metrics
        ],
        "components": [
            {"metric": _pretty_metric_name(metric_key), "component": name}
            for metric_key, names in config.disabled_components.items()
            for name in names
        ],
    }


def _shared_context(
    report: Union[Report, OrgReport], category_views: list[dict[str, Any]]
) -> dict[str, Any]:
    metrics = report.metrics
    overall = metrics.overall if metrics else None
    overall_band = BAND_META[overall.band] if overall else None
    # Radar axes are the categories — the broad shape of health.
    chart_payload = {
        "labels": [c["name"] for c in category_views if c["value"] is not None],
        "values": [c["value"] for c in category_views if c["value"] is not None],
        "color": overall_band["color"] if overall_band else "#64748b",
    }
    return {
        "report": report,
        "source": report.source,
        "generated": report.generated_at.strftime("%Y-%m-%d %H:%M UTC"),
        "overall": overall,
        "overall_band": overall_band,
        "accent": overall_band["color"] if overall_band else "#64748b",
        "category_views": category_views,
        "bands": [BAND_META[k] for k in ("excellent", "good", "moderate", "at_risk", "critical")],
        "chart_json": json.dumps(chart_payload).replace("</", "<\\/"),
        # Contacts and contributor profile enrichment are excluded here for the
        # same reason the website strips them from its report endpoint: this
        # HTML is meant to be shared, and its raw JSON card must not become a
        # harvesting surface for maintainer personal data. Contributor login,
        # account type, avatar and commit totals remain visible.
        "report_json": report.model_dump_json(
            indent=2,
            exclude={
                "data": {
                    "contacts": True,
                    "maintainership": {
                        "top_contributors": {"__all__": {"profile"}}
                    },
                }
            },
        ).replace("</", "<\\/"),
        "warnings": report.warnings,
        "metrics_version": metrics.metrics_version if metrics else None,
        "config_view": _config_view(report.config),
    }


def _ownership_view(report: Report) -> Optional[dict[str, Any]]:
    """Owner identity + the stewardship metric, for the Ownership section."""
    owner = report.data.owner
    if owner is None:
        return None
    steward = report.metrics.by_key("stewardship") if report.metrics else None
    steward_view = _metric_view(steward, REPO_METRIC_WEIGHTS) if steward else None
    return {
        "owner": owner,
        "is_org": owner.type == "Organization",
        "steward": steward_view,
    }


ECOSYSTEM_LABELS = {
    "pypi": "PyPI", "npm": "npm", "packagist": "Packagist", "crates": "crates.io",
    "go": "Go", "maven": "Maven", "rubygems": "RubyGems", "nuget": "NuGet", "hex": "Hex",
}


def _ecosystem_view(report: Report) -> list[dict[str, Any]]:
    """One row per published package for the Ecosystem section."""
    rows = []
    for p in report.data.ecosystem.packages:
        rows.append(
            {
                "ecosystem": ECOSYSTEM_LABELS.get(p.ecosystem, p.ecosystem),
                "name": p.name,
                "url": p.registry_url,
                "version": p.latest_version,
                "days": p.days_since_latest_publish,
                "monthly_downloads": p.monthly_downloads,
                "versions_count": p.versions_count,
                "license": p.license,
                "deprecated": p.is_deprecated or bool(p.latest_version_yanked),
                "deprecation_note": p.deprecation_note,
                "mismatch": p.matches_repo is False,
                "keywords": p.keywords,
            }
        )
    return rows


def _dependency_view(report: Report) -> list[dict[str, Any]]:
    """One row per declared dependency, parsed straight from manifest text —
    not resolved against a registry (no freshness/vulnerability checks yet)."""
    rows = [
        {
            "ecosystem": ECOSYSTEM_LABELS.get(d.ecosystem, d.ecosystem),
            "name": d.name,
            "version_constraint": d.version_constraint or "—",
            "manifest": d.manifest,
        }
        for d in report.data.dependencies.dependencies
    ]
    rows.sort(key=lambda r: (r["ecosystem"], r["name"].lower()))
    return rows


def _score_color(score: Optional[int]) -> str:
    """Traffic-light color for a 0..10 Scorecard check score (grey if None)."""
    if score is None:
        return "#94a3b8"
    if score >= 8:
        return "#10b981"
    if score >= 5:
        return "#f59e0b"
    if score >= 3:
        return "#f97316"
    return "#ef4444"


def _scorecard_view(report: Report) -> Optional[dict[str, Any]]:
    """OpenSSF Scorecard result for a dedicated report section, or None."""
    sc = report.data.security_signals.scorecard
    if sc is None:
        return None
    rows = [
        {
            "name": c.name,
            "score": c.score,
            "score_label": f"{c.score}/10" if c.score is not None else "n/a",
            "color": _score_color(c.score),
            "inconclusive": c.score is None,
            "reason": c.reason,
            "url": c.documentation_url,
        }
        for c in sc.checks
    ]
    rows.sort(key=lambda r: (r["score"] is not None, -(r["score"] or 0), r["name"]))
    return {
        "aggregate": sc.aggregate_score,
        "aggregate_label": f"{sc.aggregate_score:g}/10" if sc.aggregate_score is not None else "—",
        "version": sc.scorecard_version,
        "ran_at": sc.ran_at.strftime("%Y-%m-%d") if sc.ran_at else None,
        "checks": rows,
    }


def render_html(report: Report) -> str:
    metrics = report.metrics
    category_views = _category_views(
        metrics.categories if metrics else [], REPO_METRIC_WEIGHTS
    )
    context = _shared_context(report, category_views)
    ecosystems = ranked_ecosystems(
        report.data.ecosystem.packages, report.data.dependencies.ecosystems
    )
    context.update(
        data=report.data,
        repo_url=f"https://github.com/{report.source.owner}/{report.source.name}",
        ownership=_ownership_view(report),
        ecosystem_chips=[ECOSYSTEM_LABELS.get(e, e) for e in ecosystems[:3]],
        ecosystem_packages=_ecosystem_view(report),
        dependencies=_dependency_view(report),
        scorecard=_scorecard_view(report),
    )
    return _env.get_template("report.html.j2").render(**context)


def render_org_html(report: OrgReport) -> str:
    metrics = report.metrics
    category_views = _category_views(
        metrics.categories if metrics else [], ORG_METRIC_WEIGHTS
    )
    context = _shared_context(report, category_views)
    context.update(
        info=report.data.info,
        portfolio=report.data.portfolio,
        org_url=f"https://github.com/{report.source.login}",
    )
    return _env.get_template("org.html.j2").render(**context)
