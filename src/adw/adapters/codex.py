"""Codex CLI backend: `codex exec --json` (JSONL event stream).

Docs: https://developers.openai.com/codex/noninteractive
Session id arrives in the `thread.started` event as `thread_id`;
resume via `codex exec resume <thread_id>`.
"""

from __future__ import annotations

import json
from typing import Any

from adw.adapters.base import AgentAdapter, AgentInvocation, AgentResult
from adw.config import CodexOpts


class CodexAdapter(AgentAdapter):
    name = "codex"

    opts: CodexOpts

    def build_command(self, inv: AgentInvocation) -> list[str]:
        opts = self.opts
        cmd = [opts.binary, "exec"]
        if inv.session_id:
            cmd += ["resume", inv.session_id]
        cmd += ["--json", "--skip-git-repo-check"]
        sandbox = "read-only" if inv.read_only else opts.sandbox
        cmd += ["--sandbox", sandbox]
        if opts.dangerous and not inv.read_only:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        if inv.model:
            cmd += ["-m", inv.model]
        cmd += opts.extra_args
        cmd += [inv.prompt]
        return cmd

    def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)

        session_id: str | None = None
        messages: list[str] = []
        completed = False
        failed_reason = ""
        for event in events:
            etype = str(event.get("type", ""))
            if etype == "thread.started":
                session_id = event.get("thread_id") or session_id
            elif etype == "turn.completed":
                completed = True
            elif etype in ("turn.failed", "error"):
                failed_reason = json.dumps(event.get("error", event))[:500]
            elif etype.startswith("item."):
                item = event.get("item") or {}
                if item.get("type") in ("agent_message", "assistant_message"):
                    text = item.get("text") or item.get("content") or ""
                    if text:
                        messages.append(str(text))

        ok = exit_code == 0 and completed and not failed_reason
        error = ""
        if failed_reason:
            error = f"codex turn failed: {failed_reason}"
        elif exit_code == 0 and not completed:
            error = "codex stream ended without turn.completed"
        return AgentResult(
            ok=ok,
            output="\n\n".join(messages) if messages else stdout if not events else "",
            session_id=session_id,
            exit_code=exit_code,
            duration_s=0.0,
            raw=events or stdout,
            stderr_tail=stderr[-2000:],
            error=error,
        )
