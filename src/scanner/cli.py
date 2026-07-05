"""Command-line interface: scan a public GitHub repo, emit JSON / HTML reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from .collect import scan_repository
from .github import GitHubError, resolve_token
from .models import Report
from .render import render_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inspect-scan",
        description="Audit a public GitHub repository and produce JSON / HTML reports.",
    )
    parser.add_argument(
        "target",
        help="Repository URL (https://github.com/owner/name, "
        "git@github.com:owner/name.git), owner/name shorthand, or the path of a "
        "previously generated JSON report to re-render without re-scanning",
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


def _load_or_scan(args: argparse.Namespace) -> Report:
    target = Path(args.target)
    if args.target.lower().endswith(".json") and target.is_file():
        return Report.model_validate_json(target.read_text(encoding="utf-8"))
    token = resolve_token(args.token)
    if not token:
        print(
            "note: no GitHub token found (--token, GITHUB_TOKEN/GH_TOKEN env var, "
            "or .env file); using the unauthenticated 60 requests/hour limit",
            file=sys.stderr,
        )
    return scan_repository(args.target, token=token)


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

    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(f"JSON report written to {args.output}", file=sys.stderr)
    elif not args.html:
        # JSON goes to stdout only when no file output was requested at all.
        print(payload)

    if args.html:
        args.html.write_text(render_html(report), encoding="utf-8")
        print(f"HTML report written to {args.html}", file=sys.stderr)

    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
