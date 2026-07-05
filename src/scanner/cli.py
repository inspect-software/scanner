"""Command-line interface: scan a public GitHub repo, emit a JSON report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .collect import scan_repository
from .github import GitHubError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inspect-scan",
        description="Audit a public GitHub repository and produce a JSON report.",
    )
    parser.add_argument(
        "repo",
        help="Repository URL (https://github.com/owner/name, "
        "git@github.com:owner/name.git) or owner/name shorthand",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write the JSON report to this file (default: stdout)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub API token (default: GITHUB_TOKEN environment variable). "
        "Unauthenticated requests are limited to 60/hour.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact single-line JSON instead of pretty-printed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        report = scan_repository(args.repo, token=args.token)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = report.model_dump_json(indent=None if args.compact else 2)

    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(payload)

    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
