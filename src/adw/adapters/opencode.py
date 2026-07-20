"""opencode backend: `opencode run --format json`.

Docs: https://opencode.ai/docs/cli/
Resume via `opencode run --session <id>`; read-only work uses the built-in
`plan` agent (edits disabled) selected with `--agent`.
"""

from __future__ import annotations

import json
from typing import Any

from adw.adapters.base import AgentAdapter, AgentInvocation, AgentResult, TokenUsage
from adw.config import OpencodeOpts

_SESSION_KEYS = ("sessionID", "session_id", "sessionId")
_USAGE_KEYS = ("tokens", "usage")


class OpencodeAdapter(AgentAdapter):
    name = "opencode"

    opts: OpencodeOpts

    def build_command(self, inv: AgentInvocation) -> list[str]:
        opts = self.opts
        cmd = [opts.binary, "run", "--format", "json"]
        if inv.model:
            cmd += ["--model", inv.model]
        if inv.session_id:
            cmd += ["--session", inv.session_id]
        if inv.read_only:
            cmd += ["--agent", opts.readonly_agent]
        elif opts.auto:
            cmd += ["--dangerously-skip-permissions"]
        cmd += opts.extra_args
        cmd += [inv.prompt]
        return cmd

    def parse_output(self, stdout: str, stderr: str, exit_code: int) -> AgentResult:
        events = _parse_events(stdout)
        session_id = _find_session_id(events)
        texts: list[str] = []
        for event in events:
            _collect_text(event, texts)
        output = "\n".join(texts).strip() or (stdout.strip() if not events else "")
        # opencode exits 0 even when the model call fails; error events are the signal
        error = ""
        if exit_code != 0:
            error = f"opencode exited with {exit_code}"
        else:
            for event in events:
                if isinstance(event, dict) and event.get("type") == "error":
                    detail = event.get("error")
                    error = f"opencode error event: {json.dumps(detail)[:500]}"
                    break
        return AgentResult(
            ok=exit_code == 0 and not error,
            output=output,
            session_id=session_id,
            exit_code=exit_code,
            duration_s=0.0,
            tokens=_find_usage(events),
            raw=events or stdout,
            stderr_tail=stderr[-2000:],
            error=error,
        )


def _parse_events(stdout: str) -> list[Any]:
    """Accept either one JSON document or JSON-lines."""
    stdout = stdout.strip()
    if not stdout:
        return []
    try:
        doc = json.loads(stdout)
        return doc if isinstance(doc, list) else [doc]
    except json.JSONDecodeError:
        pass
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _find_session_id(node: Any) -> str | None:
    """Depth-first search for a session id key anywhere in the event payloads."""
    if isinstance(node, dict):
        for key in _SESSION_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
        for value in node.values():
            found = _find_session_id(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_session_id(item)
            if found:
                return found
    return None


def _find_usage(node: Any) -> TokenUsage | None:
    """Depth-first search for a `tokens`/`usage` dict anywhere in the event payloads.

    Best-effort: the schema varies by opencode version/provider, so anything
    unrecognized degrades to None (rendered as a dash, never a fabricated count).
    """
    if isinstance(node, dict):
        for key in _USAGE_KEYS:
            usage = _usage_from(node.get(key))
            if usage is not None:
                return usage
        for value in node.values():
            found = _find_usage(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_usage(item)
            if found is not None:
                return found
    return None


def _count(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int | float) and value >= 0:
            return int(value)
    return None


def _usage_from(data: Any) -> TokenUsage | None:
    """Map known opencode usage-key aliases to TokenUsage; None when unrecognized."""
    if not isinstance(data, dict):
        return None
    counts: dict[str, int] = {}
    for field, keys in (
        ("input_tokens", ("input", "input_tokens")),
        ("output_tokens", ("output", "output_tokens")),
        ("cache_read_tokens", ("cache_read",)),
        ("cache_write_tokens", ("cache_write",)),
    ):
        value = _count(data, *keys)
        if value is not None:
            counts[field] = value
    cache = data.get("cache")
    if isinstance(cache, dict):
        for field, key in (("cache_read_tokens", "read"), ("cache_write_tokens", "write")):
            value = _count(cache, key)
            if value is not None:
                counts[field] = value
    if "input_tokens" not in counts and "output_tokens" not in counts:
        return None
    return TokenUsage(**counts)


def _collect_text(node: Any, out: list[str]) -> None:
    """Collect assistant text parts from opencode event payloads."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                out.append(text)
        for value in node.values():
            _collect_text(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_text(item, out)
