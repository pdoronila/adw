"""`adw queue watch` — guards via the CLI, poll loop driven directly."""

from __future__ import annotations

import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from adw import cli
from adw.cli import app
from adw.queue import tickets as ticket_mod

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _adw_yaml(fake: Path, isolation: str) -> str:
    return f"""
gates:
  marker: {{command: "test -f marker.txt", timeout: 10}}
agents:
  default: {{backend: claude-code, model: sonnet}}
workflow: {{max_fix_iterations: 2, gate_order: [marker]}}
isolation: {{type: {isolation}}}
backends:
  claude-code: {{binary: "{fake}"}}
"""


@pytest.fixture()
def repo(target_repo: Path) -> Path:
    fake = FIXTURES / "fake_agent.py"
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (target_repo / "adw.yaml").write_text(_adw_yaml(fake, "worktree"))
    subprocess.run(["git", "add", "-A"], cwd=target_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cfg"], cwd=target_repo, check=True, capture_output=True)
    return target_repo


def _wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_watch_requires_isolation(target_repo: Path) -> None:
    fake = FIXTURES / "fake_agent.py"
    (target_repo / "adw.yaml").write_text(_adw_yaml(fake, "local"))
    result = runner.invoke(app, ["queue", "watch", "--repo", str(target_repo)])
    assert result.exit_code == 2
    assert "worktree" in result.output


def test_watch_parallel_requires_yes(repo: Path) -> None:
    result = runner.invoke(app, ["queue", "watch", "--parallel", "2", "--repo", str(repo)])
    assert result.exit_code == 2


def test_watch_claims_new_ticket(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("ADW_ASSUME_YES", "1")
    stop = threading.Event()
    thread = threading.Thread(
        target=cli._watch_loop, args=(repo, 1, 0.05, False, True, stop), daemon=True
    )
    thread.start()
    ticket_mod.write_ticket(repo, "task a", "do it")  # written AFTER the watcher starts
    done = repo / ".adw" / "tickets" / "done"
    assert _wait_for(lambda: len(list(done.glob("*.md"))) == 1), "ticket never processed"
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_watch_respects_parallel_cap(tmp_path, monkeypatch) -> None:
    lock, state = threading.Lock(), {"active": 0, "max": 0}

    def fake_process(ticket, repo, auto_approve_plan, yes):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.2)
        with lock:
            state["active"] -= 1
        ticket_mod.finish(ticket, repo, "shipped", "test", "r0")
        return SimpleNamespace(status="shipped")

    monkeypatch.setattr(cli, "_process_ticket", fake_process)
    for i in range(5):
        ticket_mod.write_ticket(tmp_path, f"task {i}", "body")
    stop = threading.Event()
    thread = threading.Thread(
        target=cli._watch_loop, args=(tmp_path, 2, 0.05, False, True, stop), daemon=True
    )
    thread.start()
    done = tmp_path / ".adw" / "tickets" / "done"
    assert _wait_for(lambda: len(list(done.glob("*.md"))) == 5)
    stop.set()
    thread.join(timeout=10)
    assert state["max"] == 2  # both workers ran, never more than the cap


def test_watch_clean_shutdown(tmp_path, monkeypatch) -> None:
    stop = threading.Event()

    def fake_process(ticket, repo, auto_approve_plan, yes):
        stop.set()  # shutdown arrives while this ticket is in flight
        ticket_mod.finish(ticket, repo, "shipped", "test", "r0")
        return SimpleNamespace(status="shipped")

    monkeypatch.setattr(cli, "_process_ticket", fake_process)
    ticket_mod.write_ticket(tmp_path, "first", "body")
    ticket_mod.write_ticket(tmp_path, "second", "body")
    results = cli._watch_loop(tmp_path, 1, 0.05, False, True, stop)
    root = tmp_path / ".adw" / "tickets"
    assert len(results) == 1  # in-flight ticket finished…
    assert len(list((root / "done").glob("*.md"))) == 1
    assert len(list((root / "queue").glob("*.md"))) == 1  # …second was never claimed
    assert not list((root / "in_progress").glob("*.md"))
