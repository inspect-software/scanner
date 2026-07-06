"""Report storage: standardized on-disk layout and file naming.

Layout (root customizable via --storage or the SCANNER_STORAGE variable in
the environment or a .env file):

    <storage>/
        repos/<owner>__<repo>.json     # repository reports
        repos/<owner>__<repo>.html
        orgs/<login>.json              # organization reports
        orgs/<login>.html

Names are derived from the GitHub identifiers, sanitized to filesystem-safe
characters. One file per target — a rescan overwrites the previous report.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union

from dotenv import dotenv_values

from .models import OrgReport, Report

STORAGE_ENV_VAR = "SCANNER_STORAGE"
DEFAULT_STORAGE = "storage"

REPOS_SUBDIR = "repos"
ORGS_SUBDIR = "orgs"


def storage_from_env(env_file: str = ".env") -> Optional[str]:
    """SCANNER_STORAGE from the environment, then from a .env file."""
    value = os.environ.get(STORAGE_ENV_VAR)
    if value:
        return value
    return dotenv_values(env_file).get(STORAGE_ENV_VAR) or None


def resolve_storage(cli_value: Optional[str]) -> Optional[Path]:
    """Resolve the storage root, or None when storage is disabled.

    ``cli_value`` is the --storage argument: None means the flag was absent
    (storage enabled only if SCANNER_STORAGE is set), "" means the flag was
    given without a directory (use SCANNER_STORAGE or the default), and any
    other string is an explicit directory.
    """
    if cli_value:
        return Path(cli_value)
    env_value = storage_from_env()
    if cli_value == "":
        return Path(env_value or DEFAULT_STORAGE)
    return Path(env_value) if env_value else None


def safe_name(value: str) -> str:
    """Sanitize a GitHub identifier into a filesystem-safe file name part."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip(".-")
    return cleaned.lower() or "unnamed"


def repo_report_basename(owner: str, name: str) -> str:
    return f"{safe_name(owner)}__{safe_name(name)}"


def org_report_basename(login: str) -> str:
    return safe_name(login)


def report_paths(storage: Path, report: Union[Report, OrgReport]) -> tuple[Path, Path]:
    """Standardized (json_path, html_path) for a report inside the storage root."""
    if report.report_type == "organization":
        base = storage / ORGS_SUBDIR / org_report_basename(report.source.login)
    else:
        base = storage / REPOS_SUBDIR / repo_report_basename(
            report.source.owner, report.source.name
        )
    # Append (not with_suffix): repo names may contain dots, e.g. widget.js
    return base.parent / (base.name + ".json"), base.parent / (base.name + ".html")


def store_report(
    storage: Path, report: Union[Report, OrgReport], json_payload: str, html_payload: str
) -> tuple[Path, Path]:
    """Write both report files into the storage layout; returns their paths."""
    json_path, html_path = report_paths(storage, report)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json_payload + "\n", encoding="utf-8")
    html_path.write_text(html_payload, encoding="utf-8")
    return json_path, html_path
