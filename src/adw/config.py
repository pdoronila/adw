"""Configuration for ADW: per-repo adw.yaml merged over global defaults.

Merge precedence (lowest to highest): built-in defaults < ~/.config/adw/config.yaml
< <repo>/adw.yaml < CLI overrides.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

REPO_CONFIG_NAME = "adw.yaml"
GLOBAL_CONFIG_PATH = Path("~/.config/adw/config.yaml").expanduser()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GateConfig(StrictModel):
    command: str
    timeout: int = 600


class RoleAgent(StrictModel):
    backend: str = "claude-code"
    model: str | None = None


class AgentsConfig(StrictModel):
    default: RoleAgent = RoleAgent()
    roles: dict[str, RoleAgent] = Field(default_factory=dict)


class WorkflowConfig(StrictModel):
    max_fix_iterations: int = 3
    gate_order: list[str] | None = None  # None -> declared order of `gates`
    agent_timeout: int = 1800


class ShipConfig(StrictModel):
    branch_prefix: str = "adw/"
    create_pr: bool = False


class BackendOpts(StrictModel):
    binary: str
    extra_args: list[str] = Field(default_factory=list)


class ClaudeCodeOpts(BackendOpts):
    binary: str = "claude"
    permission_mode: str = "acceptEdits"
    allowed_tools: str = "Bash,Read,Edit,Write,Glob,Grep"
    readonly_tools: str = "Read,Glob,Grep"
    bare: bool = False
    dangerous: bool = False


class CodexOpts(BackendOpts):
    binary: str = "codex"
    sandbox: str = "workspace-write"
    dangerous: bool = False


class OpencodeOpts(BackendOpts):
    binary: str = "opencode"
    auto: bool = True
    readonly_agent: str = "plan"  # opencode's built-in read-only agent


class BackendsConfig(StrictModel):
    claude_code: ClaudeCodeOpts = Field(default_factory=ClaudeCodeOpts, alias="claude-code")
    codex: CodexOpts = Field(default_factory=CodexOpts)
    opencode: OpencodeOpts = Field(default_factory=OpencodeOpts)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def for_backend(self, name: str) -> BackendOpts:
        key = name.replace("-", "_")
        opts = getattr(self, key, None)
        if opts is None:
            raise KeyError(f"unknown backend {name!r}")
        return opts  # type: ignore[no-any-return]


class AdwConfig(StrictModel):
    version: int = 1
    gates: dict[str, GateConfig] = Field(default_factory=dict)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    ship: ShipConfig = Field(default_factory=ShipConfig)
    backends: BackendsConfig = Field(default_factory=BackendsConfig)

    def gate_order(self) -> list[str]:
        return self.workflow.gate_order or list(self.gates)

    def resolve_role(self, role: str) -> RoleAgent:
        return self.agents.roles.get(role, self.agents.default)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_config(
    repo_dir: Path,
    overrides: dict[str, Any] | None = None,
    global_path: Path | None = None,
) -> AdwConfig:
    """Load and merge configuration for a target repo."""
    if global_path is None:
        env_global = os.environ.get("ADW_GLOBAL_CONFIG")
        global_path = Path(env_global) if env_global else GLOBAL_CONFIG_PATH
    data: dict[str, Any] = {}
    layers = (_load_yaml(global_path), _load_yaml(repo_dir / REPO_CONFIG_NAME), overrides or {})
    for layer in layers:
        data = _deep_merge(data, layer)
    return AdwConfig.model_validate(data)
