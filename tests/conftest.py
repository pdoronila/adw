from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adw.config import AdwConfig


@pytest.fixture()
def target_repo(tmp_path: Path) -> Path:
    """A tiny git repo with one commit, standing in for a real project."""
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "app.py").write_text("def hello():\n    return 'hello'\n")
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture()
def base_config() -> AdwConfig:
    return AdwConfig.model_validate(
        {
            "gates": {"lint": {"command": "true", "timeout": 10}},
            "agents": {"default": {"backend": "claude-code", "model": "sonnet"}},
        }
    )


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's real ~/.config/adw/config.yaml out of tests."""
    monkeypatch.setenv("ADW_GLOBAL_CONFIG", str(tmp_path / "no-global.yaml"))
