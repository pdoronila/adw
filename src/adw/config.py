"""Configuration for ADW: per-repo adw.yaml merged over global defaults.

Merge precedence (lowest to highest): built-in defaults < ~/.config/adw/config.yaml
< <repo>/adw.yaml < CLI overrides.

Run state lives under ~/.adw/ by default (see run_state.runs_root).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_CONFIG_NAME = "adw.yaml"
GLOBAL_CONFIG_PATH = Path("~/.config/adw/config.yaml").expanduser()
DATA_HOME_PATH = Path("~/.adw").expanduser()


def data_home() -> Path:
    """User-level adw data root, overridable via ADW_DATA_HOME (tests, odd setups)."""
    override = os.environ.get("ADW_DATA_HOME")
    return Path(override).expanduser() if override else DATA_HOME_PATH


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GateConfig(StrictModel):
    command: str
    timeout: int = 600


class RoleAgent(StrictModel):
    backend: str = "claude-code"
    model: str | None = None
    expert: str | None = None  # name of an entry in AdwConfig.experts


class AgentsConfig(StrictModel):
    default: RoleAgent = RoleAgent()
    roles: dict[str, RoleAgent] = Field(default_factory=dict)


class WorkflowConfig(StrictModel):
    max_fix_iterations: int = 3
    max_review_iterations: int = 2
    gate_order: list[str] | None = None  # None -> declared order of `gates`
    agent_timeout: int = 1800


class ShipConfig(StrictModel):
    branch_prefix: str = "adw/"
    create_pr: bool = False
    # Auto-integrate the work branch into the base branch after ship
    # (rebase + ff-only merge). Takes precedence over create_pr: when both
    # are true, the run lands and skips PR creation.
    land: bool = False


class NotifyConfig(StrictModel):
    macos: bool = False
    webhook: str | None = None


class IsolationConfig(StrictModel):
    # local: work in the main tree (default). worktree: a git worktree per run.
    # container: an Apple `container` per run (see ContainerEnv).
    type: Literal["local", "worktree", "container"] = "local"
    worktrees_dir: str = ".adw/worktrees"
    # container-only knobs (used when type == container)
    image: str = "adw-sandbox"
    binary: str = "container"
    # Forwarded into the container as `-e NAME` (host value). Whichever are set
    # authenticate the in-container agent; CLAUDE_CODE_OAUTH_TOKEN bills a Max/Pro
    # plan, ANTHROPIC_API_KEY the API. Only names present in the host env are sent.
    secrets: list[str] = Field(
        default_factory=lambda: ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"]
    )
    workdir: str = "/work"


class QueueConfig(StrictModel):
    file_failures: bool = False  # auto-file an investigation ticket when a run fails


class LimitsConfig(StrictModel):
    max_cost_usd: float | None = None  # pause the run when total cost exceeds this


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
    isolation: IsolationConfig = Field(default_factory=IsolationConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    # name -> system instructions ("agent experts"); loaded from experts/*.md + inline
    experts: dict[str, str] = Field(default_factory=dict)

    def gate_order(self) -> list[str]:
        return self.workflow.gate_order or list(self.gates)

    def resolve_role(self, role: str, workflow: str | None = None) -> RoleAgent:
        """Resolve a role, preferring a workflow-scoped override (`<workflow>:<role>`)."""
        if workflow:
            scoped = self.agents.roles.get(f"{workflow}:{role}")
            if scoped is not None:
                return scoped
        return self.agents.roles.get(role, self.agents.default)

    def expert_text(self, role_agent: RoleAgent) -> str | None:
        if role_agent.expert is None:
            return None
        return self.experts[role_agent.expert]

    @model_validator(mode="after")
    def _check_expert_refs(self) -> AdwConfig:
        for name, role_agent in self.agents.roles.items():
            if role_agent.expert is not None and role_agent.expert not in self.experts:
                raise ValueError(
                    f"role {name!r} references unknown expert {role_agent.expert!r}; "
                    f"define it in experts/{role_agent.expert}.md or the experts: map"
                )
        default_expert = self.agents.default.expert
        if default_expert and default_expert not in self.experts:
            raise ValueError(f"default agent references unknown expert {default_expert!r}")
        return self


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
    # Merge file-based experts (experts/<name>.md); inline `experts:` entries win.
    file_experts = _load_experts_dir(repo_dir / "experts")
    if file_experts:
        data["experts"] = {**file_experts, **data.get("experts", {})}
    return AdwConfig.model_validate(data)


def _load_experts_dir(experts_dir: Path) -> dict[str, str]:
    if not experts_dir.is_dir():
        return {}
    return {p.stem: p.read_text() for p in sorted(experts_dir.glob("*.md"))}
