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
from .models import OrgReport, Report
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
    return parser


def _load_report_file(path: Path) -> AnyReport:
    raw = path.read_text(encoding="utf-8")
    kind = json.loads(raw).get("report_type", "repository")
    model = OrgReport if kind == "organization" else Report
    return model.model_validate_json(raw)


def _load_or_scan(args: argparse.Namespace) -> AnyReport:
    target_path = Path(args.target)
    if args.target.lower().endswith(".json") and target_path.is_file():
        return _load_report_file(target_path)

    kind, *ids = parse_target(args.target)
    token = resolve_token(args.token)
    if not token:
        print(
            "note: no GitHub token found (--token, GITHUB_TOKEN/GH_TOKEN env var, "
            "or .env file); using the unauthenticated 60 requests/hour limit",
            file=sys.stderr,
        )
    if kind == "org":
        return scan_organization(ids[0], token=token)
    return scan_repository(args.target, token=token)


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
