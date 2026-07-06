from datetime import datetime, timezone
from pathlib import Path

import pytest

from scanner.models import (
    OrgData,
    OrgInfo,
    OrgRef,
    OrgReport,
    RepoRef,
    Report,
)
from scanner.storage import (
    org_report_basename,
    repo_report_basename,
    report_paths,
    resolve_storage,
    safe_name,
    store_report,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SCANNER_STORAGE", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_safe_name():
    assert safe_name("Nayjest") == "nayjest"
    assert safe_name("ai-microcore") == "ai-microcore"
    assert safe_name("weird/na me:*?") == "weird-na-me"
    assert safe_name("..leading.dots..") == "leading.dots"
    assert safe_name("///") == "unnamed"


def test_basenames():
    assert repo_report_basename("Nayjest", "ai-microcore") == "nayjest__ai-microcore"
    assert org_report_basename("Anthropics") == "anthropics"


def _repo_report() -> Report:
    return Report(
        generated_at=datetime.now(timezone.utc),
        source=RepoRef(url="acme/widget", owner="Acme", name="Widget.js"),
    )


def _org_report() -> OrgReport:
    return OrgReport(
        generated_at=datetime.now(timezone.utc),
        source=OrgRef(url="https://github.com/acme", login="Acme"),
        data=OrgData(info=OrgInfo(login="Acme")),
    )


def test_report_paths_layout(tmp_path):
    json_path, html_path = report_paths(tmp_path, _repo_report())
    assert json_path == tmp_path / "repos" / "acme__widget.js.json"
    assert html_path == tmp_path / "repos" / "acme__widget.js.html"

    json_path, html_path = report_paths(tmp_path, _org_report())
    assert json_path == tmp_path / "orgs" / "acme.json"
    assert html_path == tmp_path / "orgs" / "acme.html"


def test_store_report_writes_both_files(tmp_path):
    json_path, html_path = store_report(tmp_path / "st", _repo_report(), "{}", "<html>")
    assert json_path.read_text(encoding="utf-8") == "{}\n"
    assert html_path.read_text(encoding="utf-8") == "<html>"


def test_resolve_storage_disabled_by_default():
    assert resolve_storage(None) is None


def test_resolve_storage_flag_without_dir_uses_default():
    assert resolve_storage("") == Path("storage")


def test_resolve_storage_flag_with_dir():
    assert resolve_storage("D:/reports") == Path("D:/reports")


def test_resolve_storage_env_enables(monkeypatch):
    monkeypatch.setenv("SCANNER_STORAGE", "env-storage")
    assert resolve_storage(None) == Path("env-storage")
    # flag without dir prefers env var too
    assert resolve_storage("") == Path("env-storage")
    # explicit CLI dir beats env
    assert resolve_storage("cli-storage") == Path("cli-storage")


def test_resolve_storage_dotenv(tmp_path):
    (tmp_path / ".env").write_text("SCANNER_STORAGE=dotenv-storage\n", encoding="utf-8")
    assert resolve_storage(None) == Path("dotenv-storage")
