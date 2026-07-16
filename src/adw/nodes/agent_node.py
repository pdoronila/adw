"""Agent nodes: resolve a role to (backend, model), invoke, persist the transcript."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from adw.adapters import get_adapter
from adw.adapters.base import AgentAdapter, AgentInvocation, AgentResult
from adw.config import AdwConfig, RoleAgent

AdapterFactory = Callable[[str, str], AgentAdapter]  # (role, backend) -> adapter


class AgentRunner:
    """The single path through which workflows talk to agents.

    `adapter_factory` is the test seam: workflow tests inject per-role
    MockAdapters without touching any CLI.
    """

    def __init__(
        self,
        config: AdwConfig,
        run_dir: Path,
        adapter_factory: AdapterFactory | None = None,
        workflow: str | None = None,
    ):
        self.config = config
        self.run_dir = run_dir
        self.workflow = workflow
        self._factory = adapter_factory or (lambda _role, backend: get_adapter(backend, config))
        # Continue transcript numbering across a resumed run.
        agent_dir = run_dir / "agent"
        self._step = len(list(agent_dir.glob("*.json"))) if agent_dir.is_dir() else 0

    def run(
        self,
        role: str,
        prompt: str,
        *,
        cwd: Path,
        step_name: str,
        session_id: str | None = None,
        read_only: bool = False,
    ) -> AgentResult:
        role_agent = self.config.resolve_role(role, self.workflow)
        adapter = self._factory(role, role_agent.backend)
        # An "agent expert" prepends persistent specialized instructions to the prompt.
        expert = self.config.expert_text(role_agent)
        full_prompt = f"{expert}\n\n---\n\n{prompt}" if expert else prompt
        inv = AgentInvocation(
            prompt=full_prompt,
            cwd=cwd,
            model=role_agent.model,
            session_id=session_id,
            read_only=read_only,
            timeout_s=self.config.workflow.agent_timeout,
        )
        result = adapter.invoke(inv)
        self._persist(step_name, role, role_agent, inv, result)
        return result

    def _persist(
        self,
        step_name: str,
        role: str,
        role_agent: RoleAgent,
        inv: AgentInvocation,
        result: AgentResult,
    ) -> None:
        self._step += 1
        agent_dir = self.run_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        artifact = {
            "role": role,
            "backend": role_agent.backend,
            "model": inv.model,
            "expert": role_agent.expert,
            "resumed_session": inv.session_id,
            "read_only": inv.read_only,
            "prompt": inv.prompt,
            **result.to_artifact(),
        }
        path = agent_dir / f"{self._step:02d}-{step_name}.json"
        path.write_text(json.dumps(artifact, indent=2, default=str))
