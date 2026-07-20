from __future__ import annotations

import json
from pathlib import Path

import pytest

from adw.adapters import get_adapter
from adw.adapters.base import AgentInvocation, TokenUsage
from adw.adapters.claude_code import ClaudeCodeAdapter
from adw.adapters.codex import CodexAdapter
from adw.adapters.opencode import OpencodeAdapter
from adw.config import AdwConfig, ClaudeCodeOpts, CodexOpts, OpencodeOpts

CWD = Path("/tmp/x")


def inv(**kwargs: object) -> AgentInvocation:
    return AgentInvocation(prompt="do the thing", cwd=CWD, **kwargs)  # type: ignore[arg-type]


class TestClaudeCommand:
    adapter = ClaudeCodeAdapter(ClaudeCodeOpts())

    def test_start(self) -> None:
        cmd = self.adapter.build_command(inv(model="sonnet"))
        assert cmd[:5] == ["claude", "-p", "do the thing", "--output-format", "json"]
        assert "--model" in cmd and "sonnet" in cmd
        assert "--resume" not in cmd
        assert "--permission-mode" in cmd

    def test_resume(self) -> None:
        cmd = self.adapter.build_command(inv(session_id="sess-1"))
        assert cmd[cmd.index("--resume") + 1] == "sess-1"

    def test_read_only_limits_tools(self) -> None:
        cmd = self.adapter.build_command(inv(read_only=True))
        assert cmd[cmd.index("--allowedTools") + 1] == "Read,Glob,Grep"
        assert "--permission-mode" not in cmd
        assert "--dangerously-skip-permissions" not in cmd

    def test_dangerous_opt_in(self) -> None:
        adapter = ClaudeCodeAdapter(ClaudeCodeOpts(dangerous=True))
        assert "--dangerously-skip-permissions" in adapter.build_command(inv())


class TestClaudeParse:
    adapter = ClaudeCodeAdapter(ClaudeCodeOpts())

    def test_success(self) -> None:
        payload = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "All done.",
            "session_id": "abc-123",
            "total_cost_usd": 0.0421,
            "num_turns": 4,
        }
        result = self.adapter.parse_output(json.dumps(payload), "", 0)
        assert result.ok
        assert result.output == "All done."
        assert result.session_id == "abc-123"
        assert result.cost_usd == pytest.approx(0.0421)
        assert result.tokens is None  # no usage reported -> None, never zeros
        assert result.model_tokens == {}

    def test_usage_and_model_usage(self) -> None:
        payload = {
            "is_error": False,
            "result": "done",
            "session_id": "abc",
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 300,
                "cache_read_input_tokens": 50_000,
                "cache_creation_input_tokens": 900,
            },
            "modelUsage": {
                "claude-sonnet-4-5": {"input_tokens": 1000, "output_tokens": 250},
                "claude-haiku-4-5": {"input_tokens": 200, "output_tokens": 50},
            },
        }
        result = self.adapter.parse_output(json.dumps(payload), "", 0)
        assert result.tokens == TokenUsage(
            input_tokens=1200,
            output_tokens=300,
            cache_read_tokens=50_000,
            cache_write_tokens=900,
        )
        assert result.model_tokens == {
            "claude-sonnet-4-5": TokenUsage(input_tokens=1000, output_tokens=250),
            "claude-haiku-4-5": TokenUsage(input_tokens=200, output_tokens=50),
        }

    def test_camel_case_model_usage(self) -> None:
        payload = {
            "is_error": False,
            "result": "done",
            "session_id": "abc",
            "modelUsage": {
                "claude-sonnet-4-5": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 100,
                    "cacheCreationInputTokens": 20,
                }
            },
        }
        result = self.adapter.parse_output(json.dumps(payload), "", 0)
        assert result.model_tokens == {
            "claude-sonnet-4-5": TokenUsage(
                input_tokens=10, output_tokens=5, cache_read_tokens=100, cache_write_tokens=20
            )
        }

    def test_malformed_usage_never_raises(self) -> None:
        payload = {
            "is_error": False,
            "result": "done",
            "session_id": "abc",
            "usage": "not a dict",
            "modelUsage": {"claude-sonnet-4-5": None, "claude-haiku-4-5": {"input_tokens": -5}},
        }
        result = self.adapter.parse_output(json.dumps(payload), "", 0)
        assert result.ok
        assert result.tokens is None
        assert result.model_tokens == {}  # malformed entries skipped, never raised

    def test_is_error(self) -> None:
        payload = {"is_error": True, "result": "boom", "session_id": "abc"}
        result = self.adapter.parse_output(json.dumps(payload), "", 0)
        assert not result.ok

    def test_garbage_output(self) -> None:
        result = self.adapter.parse_output("not json at all", "stderr text", 1)
        assert not result.ok
        assert "parse" in result.error


class TestCodexCommand:
    adapter = CodexAdapter(CodexOpts())

    def test_start(self) -> None:
        cmd = self.adapter.build_command(inv(model="gpt-5-codex"))
        assert cmd[:2] == ["codex", "exec"]
        assert "resume" not in cmd
        assert "--json" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
        assert cmd[-1] == "do the thing"

    def test_resume(self) -> None:
        cmd = self.adapter.build_command(inv(session_id="thread-9"))
        assert cmd[1:4] == ["exec", "resume", "thread-9"]

    def test_read_only_sandbox(self) -> None:
        cmd = self.adapter.build_command(inv(read_only=True))
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"


class TestCodexParse:
    adapter = CodexAdapter(CodexOpts())

    def test_success_stream(self) -> None:
        lines = [
            {"type": "thread.started", "thread_id": "th_42"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Did it."}},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 3},
            },
        ]
        stdout = "\n".join(json.dumps(line) for line in lines)
        result = self.adapter.parse_output(stdout, "", 0)
        assert result.ok
        assert result.session_id == "th_42"
        assert result.output == "Did it."
        assert result.tokens == TokenUsage(input_tokens=10, output_tokens=3, cache_read_tokens=5)
        assert result.model_tokens == {}  # codex has no per-model report

    def test_usage_missing_or_malformed(self) -> None:
        for usage in (None, "junk", {}):
            lines: list[dict[str, object]] = [
                {"type": "thread.started", "thread_id": "th_45"},
                {"type": "turn.completed"},
            ]
            if usage is not None:
                lines[-1]["usage"] = usage
            stdout = "\n".join(json.dumps(line) for line in lines)
            result = self.adapter.parse_output(stdout, "", 0)
            assert result.ok
            assert result.tokens is None

    def test_turn_failed(self) -> None:
        lines = [
            {"type": "thread.started", "thread_id": "th_43"},
            {"type": "turn.failed", "error": {"message": "quota"}},
        ]
        stdout = "\n".join(json.dumps(line) for line in lines)
        result = self.adapter.parse_output(stdout, "", 0)
        assert not result.ok
        assert "failed" in result.error

    def test_no_completion_marker(self) -> None:
        stdout = json.dumps({"type": "thread.started", "thread_id": "th_44"})
        result = self.adapter.parse_output(stdout, "", 0)
        assert not result.ok


class TestOpencodeCommand:
    adapter = OpencodeAdapter(OpencodeOpts())

    def test_start(self) -> None:
        cmd = self.adapter.build_command(inv(model="anthropic/claude-sonnet-4-5"))
        assert cmd[:4] == ["opencode", "run", "--format", "json"]
        assert cmd[cmd.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
        assert "--dangerously-skip-permissions" in cmd

    def test_resume(self) -> None:
        cmd = self.adapter.build_command(inv(session_id="ses_7"))
        assert cmd[cmd.index("--session") + 1] == "ses_7"

    def test_read_only_uses_plan_agent(self) -> None:
        cmd = self.adapter.build_command(inv(read_only=True))
        assert cmd[cmd.index("--agent") + 1] == "plan"
        assert "--dangerously-skip-permissions" not in cmd


class TestOpencodeParse:
    adapter = OpencodeAdapter(OpencodeOpts())

    def test_event_stream(self) -> None:
        lines = [
            {"type": "step-start", "sessionID": "ses_abc"},
            {"type": "text", "text": "Here is the answer.", "sessionID": "ses_abc"},
        ]
        stdout = "\n".join(json.dumps(line) for line in lines)
        result = self.adapter.parse_output(stdout, "", 0)
        assert result.ok
        assert result.session_id == "ses_abc"
        assert "Here is the answer." in result.output

    def test_nested_session_id(self) -> None:
        doc = {"parts": [{"type": "text", "text": "hi"}], "info": {"sessionID": "ses_n"}}
        result = self.adapter.parse_output(json.dumps(doc), "", 0)
        assert result.session_id == "ses_n"
        assert result.ok

    def test_nonzero_exit(self) -> None:
        result = self.adapter.parse_output("", "boom", 3)
        assert not result.ok

    def test_real_nested_part_shape(self) -> None:
        # observed from opencode 1.4.10: text lives under event["part"]["text"]
        lines = [
            {
                "type": "text",
                "sessionID": "ses_real",
                "part": {"type": "text", "text": "HELLO", "sessionID": "ses_real"},
            },
            {"type": "step_finish", "sessionID": "ses_real", "part": {"type": "step-finish"}},
        ]
        stdout = "\n".join(json.dumps(line) for line in lines)
        result = self.adapter.parse_output(stdout, "", 0)
        assert result.ok
        assert result.output == "HELLO"
        assert result.session_id == "ses_real"

    def test_nested_tokens_node(self) -> None:
        # observed shape: assistant info carries tokens {input, output, cache: {read, write}}
        doc = {
            "info": {
                "sessionID": "ses_t",
                "tokens": {"input": 500, "output": 40, "cache": {"read": 900, "write": 30}},
            },
            "parts": [{"type": "text", "text": "hi"}],
        }
        result = self.adapter.parse_output(json.dumps(doc), "", 0)
        assert result.ok
        assert result.tokens == TokenUsage(
            input_tokens=500, output_tokens=40, cache_read_tokens=900, cache_write_tokens=30
        )

    def test_unrecognized_usage_degrades_to_none(self) -> None:
        doc = {"info": {"sessionID": "ses_u", "usage": {"weird": 1}}, "type": "text", "text": "hi"}
        result = self.adapter.parse_output(json.dumps(doc), "", 0)
        assert result.ok
        assert result.tokens is None

    def test_error_event_despite_exit_zero(self) -> None:
        # opencode exits 0 even when the model call fails
        line = {
            "type": "error",
            "sessionID": "ses_err",
            "error": {"name": "APIError", "data": {"message": "model does not exist"}},
        }
        result = self.adapter.parse_output(json.dumps(line), "", 0)
        assert not result.ok
        assert "error event" in result.error


def test_registry_unknown_backend(base_config: AdwConfig) -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        get_adapter("gemini-cli", base_config)


def test_missing_binary_is_clean_failure(tmp_path: Path, base_config: AdwConfig) -> None:
    adapter = ClaudeCodeAdapter(ClaudeCodeOpts(binary="/nonexistent/claude"))
    result = adapter.invoke(AgentInvocation(prompt="hi", cwd=tmp_path))
    assert not result.ok
    assert "not found" in result.error
