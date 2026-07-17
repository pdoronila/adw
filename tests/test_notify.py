"""Best-effort notify dispatch: channel payloads, no-ops, and failure isolation."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

import pytest

import adw.notify
from adw.config import AdwConfig
from adw.notify import notify
from adw.state.run_state import RunState


def make_state(status: str = "awaiting_plan_approval") -> RunState:
    return RunState(
        run_id="r1", workflow="feature", task="t", repo="/tmp/x", status=status
    )


def test_absent_config_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(("run", a)))
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: calls.append(("open", a))
    )
    notify(make_state(), AdwConfig())
    assert calls == []


def test_macos_channel_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adw.notify.sys, "platform", "darwin")
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda args, **k: captured.append(args))
    config = AdwConfig.model_validate({"notify": {"macos": True}})
    notify(make_state(), config)
    assert len(captured) == 1
    args = captured[0]
    assert args[0] == "osascript" and args[1] == "-e"
    assert "adw: r1 awaiting plan approval" in args[2]
    assert "[feature]" in args[2]


def test_macos_noop_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adw.notify.sys, "platform", "linux")
    called = False

    def _run(*a, **k):
        nonlocal called
        called = True

    monkeypatch.setattr(subprocess, "run", _run)
    notify(make_state(), AdwConfig.model_validate({"notify": {"macos": True}}))
    assert called is False


def test_webhook_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    config = AdwConfig.model_validate({"notify": {"webhook": "https://example.test/hook"}})
    notify(make_state(status="failed"), config)
    req = captured["req"]
    assert req.full_url == "https://example.test/hook"
    assert req.method == "POST"
    assert req.headers["Content-type"] == "application/json"
    assert captured["timeout"] == 3
    assert json.loads(req.data) == {
        "run_id": "r1",
        "status": "failed",
        "workflow": "feature",
        "task": "t",
        "repo": "/tmp/x",
    }


def test_raising_channel_does_not_propagate_or_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adw.notify.sys, "platform", "darwin")

    def _run(*a, **k):
        raise OSError("osascript missing")

    opened: list[object] = []
    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: opened.append(req)
    )
    config = AdwConfig.model_validate(
        {"notify": {"macos": True, "webhook": "https://example.test/hook"}}
    )
    notify(make_state(), config)  # does not raise
    assert len(opened) == 1  # webhook still ran despite macos raising


def test_raising_webhook_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    config = AdwConfig.model_validate({"notify": {"webhook": "https://example.test/hook"}})
    notify(make_state(), config)  # does not raise


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("awaiting_plan_approval", "awaiting plan approval"),
        ("awaiting_final_review", "awaiting final review"),
        ("failed", "failed"),
    ],
)
def test_message_per_status(
    monkeypatch: pytest.MonkeyPatch, status: str, label: str
) -> None:
    monkeypatch.setattr(adw.notify.sys, "platform", "darwin")
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda args, **k: captured.append(args))
    notify(make_state(status=status), AdwConfig.model_validate({"notify": {"macos": True}}))
    assert f"adw: r1 {label} [feature]" in captured[0][2]
