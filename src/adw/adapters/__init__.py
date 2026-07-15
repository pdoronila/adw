"""Adapter registry: backend name -> AgentAdapter."""

from __future__ import annotations

from adw.adapters.base import AgentAdapter, AgentInvocation, AgentResult
from adw.adapters.claude_code import ClaudeCodeAdapter
from adw.adapters.codex import CodexAdapter
from adw.adapters.opencode import OpencodeAdapter
from adw.config import AdwConfig

ADAPTERS: dict[str, type[AgentAdapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CodexAdapter.name: CodexAdapter,
    OpencodeAdapter.name: OpencodeAdapter,
}


def get_adapter(backend: str, config: AdwConfig) -> AgentAdapter:
    cls = ADAPTERS.get(backend)
    if cls is None:
        valid = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"unknown backend {backend!r}; valid backends: {valid}")
    return cls(config.backends.for_backend(backend))


__all__ = [
    "ADAPTERS",
    "AgentAdapter",
    "AgentInvocation",
    "AgentResult",
    "get_adapter",
]
