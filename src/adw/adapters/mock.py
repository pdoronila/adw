"""Scripted in-process adapter for tests: no subprocesses, no CLIs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from adw.adapters.base import AgentAdapter, AgentInvocation, AgentResult, TokenUsage
from adw.config import BackendOpts


@dataclass
class ScriptedTurn:
    output: str = "ok"
    ok: bool = True
    session_id: str = "mock-session-1"
    cost_usd: float | None = None
    tokens: TokenUsage | None = None
    model_tokens: dict[str, TokenUsage] | None = None
    on_invoke: Callable[[AgentInvocation], None] | None = None


class MockAdapter(AgentAdapter):
    name = "mock"

    def __init__(self, script: list[ScriptedTurn] | None = None):
        super().__init__(BackendOpts(binary="mock"))
        self.script = list(script or [])
        self.invocations: list[AgentInvocation] = []

    def build_command(self, inv: AgentInvocation) -> list[str]:
        return ["mock"]

    def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
        raise NotImplementedError("MockAdapter never parses subprocess output")

    def invoke(self, inv: AgentInvocation) -> AgentResult:
        self.invocations.append(inv)
        turn = self.script.pop(0) if self.script else ScriptedTurn()
        if turn.on_invoke is not None:
            turn.on_invoke(inv)
        return AgentResult(
            ok=turn.ok,
            output=turn.output,
            session_id=turn.session_id,
            cost_usd=turn.cost_usd,
            tokens=turn.tokens,
            model_tokens=dict(turn.model_tokens or {}),
            exit_code=0 if turn.ok else 1,
            duration_s=0.0,
            error="" if turn.ok else "mock failure",
        )
