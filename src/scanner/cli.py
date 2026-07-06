"""Command-line interface: scan a GitHub repo or organization, emit JSON / HTML."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Union

from pydantic import ValidationError

from .collect import scan_organization, scan_repository
from .github import GitHubError, parse_target, resolve_token
from .metrics import validate_config
from .models import OrgReport, Report, ScanConfig
from .render import render_html, render_org_html
from .storage import STORAGE_ENV_VAR, resolve_storage, store_report

AnyReport = Union[Report, OrgReport]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inspect-scan",
        description="Audit a public GitHub repository or organization and "
        "produce JSON / HTML reports.",
    )
    parser.add_argument(
        "target",
        help="Repository (https://github.com/owner/name, git@github.com:owner/name.git, "
        "or owner/name), organization (https://github.com/orgname or orgname), or the "
        "path of a previously generated JSON report to re-render without re-scanning",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write the JSON report to this file (default: stdout)",
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=None,
        metavar="PATH",
        help="Also render a single-file human-readable HTML report to this path",
    )
    parser.add_argument(
        "--storage",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="Store JSON + HTML reports under a storage folder with standardized "
        "names (repos/<owner>__<name>.*, orgs/<login>.*). Without a DIR the "
        f"location comes from {STORAGE_ENV_VAR} (environment or .env file), "
        "falling back to ./storage. Storage is also enabled automatically when "
        f"{STORAGE_ENV_VAR} is set.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub API token. When omitted, resolved from the GITHUB_TOKEN or "
        "GH_TOKEN environment variable, then from a .env file in the working "
        "directory. Unauthenticated requests are limited to 60/hour.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact single-line JSON instead of pretty-printed",
    )

    config_group = parser.add_argument_group(
        "scan configuration",
        "Enable/disable parts of the scoring methodology. Disabled items are "
        "removed from scoring and the remaining weights renormalized (never "
        "counted as zero). The resulting configuration is embedded in the report.",
    )
    config_group.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON file with a scan configuration "
        '({"disabled_categories": [...], "disabled_metrics": [...], '
        '"disabled_components": {"metric_key": ["Component name"]}}). '
        "Command-line --disable-* flags are merged on top of it.",
    )
    config_group.add_argument(
        "--disable-category",
        action="append",
        default=[],
        metavar="KEY",
        help="Exclude a whole category from scoring (repeatable), e.g. security",
    )
    config_group.add_argument(
        "--disable-metric",
        action="append",
        default=[],
        metavar="KEY",
        help="Exclude a single metric from scoring (repeatable), e.g. popularity",
    )
    config_group.add_argument(
        "--disable-component",
        action="append",
        default=[],
        metavar="METRIC:COMPONENT",
        help="Exclude one component within a metric (repeatable), e.g. "
        "popularity:Watchers",
    )
    return parser


def build_config(args: argparse.Namespace) -> ScanConfig:
    """Assemble a ScanConfig from an optional --config file plus --disable-* flags."""
    config = ScanConfig()
    if args.config is not None:
        config = ScanConfig.model_validate_json(args.config.read_text(encoding="utf-8"))

    categories = list(config.disabled_categories)
    for key in args.disable_category:
        if key not in categories:
            categories.append(key)

    metrics = list(config.disabled_metrics)
    for key in args.disable_metric:
        if key not in metrics:
            metrics.append(key)

    components = {k: list(v) for k, v in config.disabled_components.items()}
    for spec in args.disable_component:
        metric_key, sep, component = spec.partition(":")
        if not sep or not metric_key.strip() or not component.strip():
            raise ValueError(
                f"--disable-component expects METRIC:COMPONENT, got {spec!r}"
            )
        names = components.setdefault(metric_key.strip(), [])
        if component.strip() not in names:
            names.append(component.strip())

    return ScanConfig(
        disabled_categories=categories,
        disabled_metrics=metrics,
        disabled_components=components,
    )


def _config_requested(args: argparse.Namespace) -> bool:
    return bool(
        args.config or args.disable_category or args.disable_metric or args.disable_component
    )


def _load_report_file(path: Path) -> AnyReport:
    raw = path.read_text(encoding="utf-8")
    kind = json.loads(raw).get("report_type", "repository")
    model = OrgReport if kind == "organization" else Report
    return model.model_validate_json(raw)


def _load_or_scan(args: argparse.Namespace) -> AnyReport:
    target_path = Path(args.target)
    if args.target.lower().endswith(".json") and target_path.is_file():
        if _config_requested(args):
            print(
                "note: scan-configuration flags are ignored when re-rendering a "
                "stored JSON report; the report keeps the configuration it was "
                "scanned with",
                file=sys.stderr,
            )
        return _load_report_file(target_path)

    config = build_config(args)
    for warning in validate_config(config):
        print(f"warning: {warning}", file=sys.stderr)

    kind, *ids = parse_target(args.target)
    token = resolve_token(args.token)
    if not token:
        print(
            "note: no GitHub token found (--token, GITHUB_TOKEN/GH_TOKEN env var, "
            "or .env file); using the unauthenticated 60 requests/hour limit",
            file=sys.stderr,
        )
    if kind == "org":
        return scan_organization(ids[0], token=token, config=config)
    return scan_repository(args.target, token=token, config=config)


def _render(report: AnyReport) -> str:
    if isinstance(report, OrgReport):
        return render_org_html(report)
    return render_html(report)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        report = _load_or_scan(args)
    except ValidationError as exc:
        print(f"error: {args.target} is not a valid scanner report: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = report.model_dump_json(indent=None if args.compact else 2)
    storage = resolve_storage(args.storage)
    html_needed = args.html is not None or storage is not None
    html_payload = _render(report) if html_needed else None

    if storage is not None:
        json_path, html_path = store_report(storage, report, payload, html_payload)
        print(f"Stored: {json_path}", file=sys.stderr)
        print(f"Stored: {html_path}", file=sys.stderr)

    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(f"JSON report written to {args.output}", file=sys.stderr)
    elif not args.html and storage is None:
        # JSON goes to stdout only when no file output was requested at all.
        print(payload)

    if args.html:
        args.html.write_text(html_payload, encoding="utf-8")
        print(f"HTML report written to {args.html}", file=sys.stderr)

    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
