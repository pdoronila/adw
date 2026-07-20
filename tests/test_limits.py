"""Tests for the best-effort session-limit probes behind the sidebar."""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adw import limits


@pytest.fixture(autouse=True)
def _isolated_probes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fresh cache per test; probes never see the developer's real machine."""
    monkeypatch.setattr(limits, "_cache", None)
    monkeypatch.delenv("ADW_UI_NO_LIMITS", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setattr(limits, "sys", SimpleNamespace(platform="linux"))  # no Keychain


ROLLOUT_WITH_LIMITS = "\n".join(
    [
        json.dumps({"timestamp": "2026-07-20T10:00:00Z", "payload": {"type": "session_meta"}}),
        json.dumps(
            {
                "timestamp": "2026-07-20T10:05:00Z",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 12.5,
                            "window_minutes": 300,
                            "resets_at": 1_789_000_000,
                        },
                        "secondary": {"used_percent": 61.0, "window_minutes": 10080},
                    },
                },
            }
        ),
        "not json at all",
        json.dumps({"timestamp": "2026-07-20T10:06:00Z", "payload": {"type": "agent_message"}}),
    ]
)


def _write_rollout(tmp_path: Path, content: str, name: str = "rollout-1.jsonl") -> Path:
    day = tmp_path / "codex-home" / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True, exist_ok=True)
    path = day / name
    path.write_text(content)
    return path


def test_codex_limits_parses_last_token_count(tmp_path: Path) -> None:
    _write_rollout(tmp_path, ROLLOUT_WITH_LIMITS)

    out = limits.codex_limits()
    assert [(limit.backend, limit.label) for limit in out] == [
        ("codex", "5h"),
        ("codex", "weekly"),
    ]
    assert out[0].used_percent == 12.5
    assert out[0].resets_at == datetime.fromtimestamp(1_789_000_000, tz=UTC)
    assert out[0].as_of == datetime(2026, 7, 20, 10, 5, tzinfo=UTC)
    assert out[1].resets_at is None  # missing key skipped, window still renders


def test_codex_limits_no_token_count_event(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path, json.dumps({"timestamp": "2026-07-20T10:00:00Z", "payload": {"type": "x"}})
    )
    assert limits.codex_limits() == []


def test_codex_limits_no_sessions_dir() -> None:
    assert limits.codex_limits() == []  # CODEX_HOME points at nothing


def test_codex_limits_odd_window_minutes(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path,
        json.dumps(
            {
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"used_percent": 5.0, "window_minutes": 60},
                        "secondary": {"used_percent": 5.0},  # no window: skipped
                    },
                }
            }
        ),
    )
    out = limits.codex_limits()
    assert [limit.label for limit in out] == ["60m"]
    assert out[0].as_of is not None  # falls back to the file mtime


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _stub_http(
    monkeypatch: pytest.MonkeyPatch, body: bytes, error: Exception | None = None
) -> list[urllib.request.Request]:
    seen: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> _FakeResponse:
        seen.append(request)
        if error is not None:
            raise error
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_claude_limits_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "_claude_token", lambda: "tok-123")
    payload = {
        "five_hour": {"utilization": 32, "resets_at": "2026-07-20T12:00:00Z"},
        "seven_day": {"utilization": 61.4},
        "seven_day_opus": {"utilization": "not-a-number"},
    }
    seen = _stub_http(monkeypatch, json.dumps(payload).encode())

    out = limits.claude_limits()
    assert [(limit.label, limit.used_percent) for limit in out] == [("5h", 32.0), ("weekly", 61.4)]
    assert out[0].resets_at == datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    assert out[1].resets_at is None
    assert seen[0].get_header("Authorization") == "Bearer tok-123"
    assert seen[0].get_header("Anthropic-beta") == "oauth-2025-04-20"


def test_claude_limits_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "_claude_token", lambda: None)
    seen = _stub_http(monkeypatch, b"{}")
    assert limits.claude_limits() == []
    assert seen == []  # no token -> no network call


def test_claude_limits_malformed_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "_claude_token", lambda: "tok")
    _stub_http(monkeypatch, json.dumps({"five_hour": "nope", "other": {}}).encode())
    assert limits.claude_limits() == []


def test_claude_limits_http_error_degrades_via_session_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "_claude_token", lambda: "tok")
    _stub_http(monkeypatch, b"", error=OSError("boom"))
    assert limits.session_limits() == []  # probe failure never propagates


def test_claude_limits_malformed_json_degrades_via_session_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "_claude_token", lambda: "tok")
    _stub_http(monkeypatch, b"<html>login</html>")
    assert limits.session_limits() == []


def test_claude_token_from_credentials_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude-home"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-xyz"}})
    )
    assert limits._claude_token() == "sk-ant-oat-xyz"

    (config_dir / ".credentials.json").write_text("garbage")
    assert limits._claude_token() is None  # non-darwin stub: no Keychain fallback


def test_session_limits_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADW_UI_NO_LIMITS", "1")
    monkeypatch.setattr(limits, "claude_limits", _raise)
    monkeypatch.setattr(limits, "codex_limits", _raise)
    assert limits.session_limits() == []


def _raise() -> list[limits.SessionLimit]:
    raise AssertionError("probe must not run")


def test_session_limits_ttl_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(limits, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    calls = {"n": 0}

    def fake_probe() -> list[limits.SessionLimit]:
        calls["n"] += 1
        return [limits.SessionLimit(backend="codex", label="5h", used_percent=float(calls["n"]))]

    monkeypatch.setattr(limits, "claude_limits", lambda: [])
    monkeypatch.setattr(limits, "codex_limits", fake_probe)

    first = limits.session_limits()
    assert calls["n"] == 1
    clock["now"] = 30.0
    assert limits.session_limits() is first  # inside TTL: cached
    assert calls["n"] == 1
    clock["now"] = 90.0
    refreshed = limits.session_limits()  # past TTL: re-probed
    assert calls["n"] == 2
    assert refreshed[0].used_percent == 2.0
