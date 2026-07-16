"""Execution environments: host vs Apple container command construction."""

from __future__ import annotations

from pathlib import Path

from adw.config import AdwConfig
from adw.exec_env import ContainerEnv, LocalEnv, make_env


def test_make_env_selects_by_isolation() -> None:
    assert isinstance(make_env(AdwConfig()), LocalEnv)
    worktree = AdwConfig.model_validate({"isolation": {"type": "worktree"}})
    assert isinstance(make_env(worktree), LocalEnv)
    container = AdwConfig.model_validate({"isolation": {"type": "container"}})
    assert isinstance(make_env(container), ContainerEnv)


def test_container_wraps_argv_with_mount_and_secrets() -> None:
    cfg = AdwConfig.model_validate(
        {
            "isolation": {
                "type": "container",
                "image": "adw-sandbox",
                "binary": "container",
                "secrets": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
                "workdir": "/work",
            }
        }
    ).isolation
    wrapped = ContainerEnv(cfg)._wrap(["claude", "-p", "hi"], Path("/repo/x"))
    assert wrapped[:7] == ["container", "run", "--rm", "-v", "/repo/x:/work", "-w", "/work"]
    # secrets forwarded as `-e NAME` (value taken from host env at run time)
    assert wrapped[7:11] == ["-e", "ANTHROPIC_API_KEY", "-e", "OPENAI_API_KEY"]
    assert wrapped[11] == "adw-sandbox"
    assert wrapped[12:] == ["claude", "-p", "hi"]


def test_container_wraps_shell() -> None:
    cfg = AdwConfig.model_validate({"isolation": {"type": "container"}}).isolation
    wrapped = ContainerEnv(cfg)._wrap(["sh", "-lc", "pytest -q"], Path("/repo"))
    assert wrapped[-3:] == ["sh", "-lc", "pytest -q"]
    assert "adw-sandbox" in wrapped


def test_local_env_runs_on_host(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hi")
    result = LocalEnv().run_shell("cat f.txt", cwd=tmp_path, timeout=10)
    assert result.returncode == 0
    assert "hi" in result.stdout
