"""Tests for the `adw ui` CLI command's server binding.

Regression tests for the LAN-reachability bug: `adw ui` used to default to
127.0.0.1, so the dashboard was unreachable from other machines unless the
user passed --host 0.0.0.0 explicitly. These pin the --host pass-through to
uvicorn.run in both the plain and --reload paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from typer.testing import CliRunner  # noqa: E402

from adw.cli import app  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's real ~/.config/adw/repos.json out of tests."""
    monkeypatch.setenv("ADW_REPOS_FILE", str(tmp_path / "repos.json"))
    monkeypatch.delenv("ADW_UI_REPOS", raising=False)


@pytest.fixture()
def uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict[str, Any]]]:
    """Stub uvicorn.run so the server never actually starts (it would block)."""
    calls: list[tuple[tuple, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    return calls


def test_ui_passes_explicit_host_to_uvicorn(
    target_repo: Path, uvicorn_run: list[tuple[tuple, dict[str, Any]]]
) -> None:
    result = runner.invoke(
        app,
        ["ui", "--repo", str(target_repo), "--host", "10.0.0.5", "--port", "9000", "--no-open"],
    )
    assert result.exit_code == 0, result.output
    [(_, kwargs)] = uvicorn_run
    assert kwargs["host"] == "10.0.0.5"
    assert kwargs["port"] == 9000


def test_ui_default_host_binds_all_interfaces(
    target_repo: Path, uvicorn_run: list[tuple[tuple, dict[str, Any]]]
) -> None:
    result = runner.invoke(app, ["ui", "--repo", str(target_repo), "--no-open"])
    assert result.exit_code == 0, result.output
    [(_, kwargs)] = uvicorn_run
    assert kwargs["host"] == "0.0.0.0"


def test_ui_reload_passes_host_and_repos_env(
    target_repo: Path, uvicorn_run: list[tuple[tuple, dict[str, Any]]]
) -> None:
    result = runner.invoke(
        app,
        ["ui", "--repo", str(target_repo), "--host", "10.0.0.5", "--no-open", "--reload"],
    )
    assert result.exit_code == 0, result.output
    [(args, kwargs)] = uvicorn_run
    assert args == ("adw.ui.server:app_factory",)
    assert kwargs["factory"] is True
    assert kwargs["reload"] is True
    assert kwargs["host"] == "10.0.0.5"
    assert str(target_repo.resolve()) in os.environ["ADW_UI_REPOS"].split(os.pathsep)


def test_ui_opens_browser_at_localhost_not_wildcard(
    target_repo: Path,
    uvicorn_run: list[tuple[tuple, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    result = runner.invoke(app, ["ui", "--repo", str(target_repo)])
    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:8770/"]
