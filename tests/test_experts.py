"""Agent experts + workflow-scoped role resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from adw.adapters.mock import MockAdapter, ScriptedTurn
from adw.config import AdwConfig, load_config
from adw.nodes.agent_node import AgentRunner
from adw.state.run_state import create_run_dir


def test_experts_loaded_from_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "experts").mkdir(parents=True)
    (repo / "experts" / "surgeon.md").write_text("Be surgical.")
    (repo / "adw.yaml").write_text(
        "agents:\n  default: {backend: claude-code, expert: surgeon}\n"
    )
    config = load_config(repo)
    assert config.experts["surgeon"] == "Be surgical."


def test_inline_expert_overrides_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "experts").mkdir(parents=True)
    (repo / "experts" / "surgeon.md").write_text("from file")
    (repo / "adw.yaml").write_text(
        "experts: {surgeon: from inline}\nagents:\n  default: {backend: claude-code}\n"
    )
    config = load_config(repo)
    assert config.experts["surgeon"] == "from inline"


def test_unknown_expert_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown expert"):
        AdwConfig.model_validate(
            {"agents": {"roles": {"build": {"backend": "claude-code", "expert": "ghost"}}}}
        )


def test_workflow_scoped_role_resolution() -> None:
    config = AdwConfig.model_validate(
        {
            "experts": {"surgeon": "Be surgical."},
            "agents": {
                "default": {"backend": "claude-code", "model": "sonnet"},
                "roles": {
                    "build": {"backend": "claude-code", "model": "sonnet"},
                    "hotfix:build": {
                        "backend": "claude-code",
                        "model": "opus",
                        "expert": "surgeon",
                    },
                },
            },
        }
    )
    assert config.resolve_role("build").model == "sonnet"
    assert config.resolve_role("build", "feature").model == "sonnet"
    scoped = config.resolve_role("build", "hotfix")
    assert scoped.model == "opus"
    assert scoped.expert == "surgeon"
    assert config.expert_text(scoped) == "Be surgical."


def test_expert_text_prepended_to_prompt(tmp_path: Path) -> None:
    config = AdwConfig.model_validate(
        {
            "experts": {"surgeon": "SURGEON RULES: smallest diff."},
            "agents": {
                "roles": {"hotfix:build": {"backend": "claude-code", "expert": "surgeon"}},
            },
        }
    )
    mock = MockAdapter([ScriptedTurn(output="done")])
    runner = AgentRunner(
        config,
        create_run_dir(tmp_path, "r1"),
        adapter_factory=lambda role, backend: mock,
        workflow="hotfix",
    )
    runner.run("build", "Implement the fix.", cwd=tmp_path, step_name="build")
    sent = mock.invocations[0].prompt
    assert sent.startswith("SURGEON RULES: smallest diff.")
    assert "Implement the fix." in sent


def test_no_expert_leaves_prompt_unchanged(tmp_path: Path) -> None:
    config = AdwConfig.model_validate({"agents": {"default": {"backend": "claude-code"}}})
    mock = MockAdapter([ScriptedTurn(output="done")])
    runner = AgentRunner(config, create_run_dir(tmp_path, "r2"), adapter_factory=lambda r, b: mock)
    runner.run("build", "Just build.", cwd=tmp_path, step_name="build")
    assert mock.invocations[0].prompt == "Just build."
