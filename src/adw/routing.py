"""Factory router: classify a ticket → the workflow that should handle it.

An agent classifier with a deterministic keyword fallback, so routing still
works when no agent is available (or the model returns junk).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from adw.adapters import get_adapter
from adw.adapters.base import AgentAdapter, AgentInvocation
from adw.config import AdwConfig
from adw.prompts import render
from adw.workflows import WORKFLOWS

# Ordered (workflow, keywords): first match wins in the fallback.
_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("cve", ("cve", "vulnerab", "exploit", "injection", "traversal", "security advisory")),
    ("hotfix", ("production", "prod down", "outage", "crash", "500", "p0", "urgent", "hotfix")),
    ("bug", ("bug", "broken", "regression", "incorrect", "wrong", "fails", "error", "exception")),
    ("chore", ("bump", "upgrade", "rename", "typo", "chore", "cleanup", "docs", "lint", "format")),
]


@dataclass
class RouteResult:
    workflow: str
    task: str
    rationale: str
    method: Literal["agent", "fallback"]


def route(
    task: str,
    config: AdwConfig,
    repo: Path,
    *,
    adapter: AgentAdapter | None = None,
) -> RouteResult:
    """Pick a workflow for `task`. Tries the router agent, falls back to keywords."""
    names = list(WORKFLOWS)
    role = config.resolve_role("router")
    try:
        adapter = adapter or get_adapter(role.backend, config)
        prompt = render(
            "router",
            task=task,
            workflows="\n".join(f"- {n}: {WORKFLOWS[n].description}" for n in names),
        )
        result = adapter.invoke(
            AgentInvocation(
                prompt=prompt,
                cwd=repo,
                model=role.model,
                read_only=True,
                timeout_s=config.workflow.agent_timeout,
            )
        )
        if result.ok:
            parsed = _parse_route(result.output, names)
            if parsed is not None:
                workflow, refined, rationale = parsed
                return RouteResult(workflow, refined or task, rationale, "agent")
    except Exception:
        pass
    workflow = keyword_route(task, names)
    return RouteResult(workflow, task, "no agent classification; matched by keywords", "fallback")


def keyword_route(task: str, names: list[str]) -> str:
    text = task.lower()
    for workflow, keywords in _KEYWORDS:
        if workflow in names and any(k in text for k in keywords):
            return workflow
    return "feature" if "feature" in names else names[0]


def _parse_route(output: str, names: list[str]) -> tuple[str, str, str] | None:
    match = re.search(r"\{.*\}", output, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    workflow = str(data.get("workflow", "")).strip()
    if workflow not in names:
        return None
    return workflow, str(data.get("task", "")).strip(), str(data.get("rationale", "")).strip()
