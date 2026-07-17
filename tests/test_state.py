from __future__ import annotations

import json
from pathlib import Path

from adw.state.run_state import (
    RunState,
    create_run_dir,
    list_runs,
    load_state,
    new_run_id,
    save_state,
    slugify,
)


def test_slugify() -> None:
    assert slugify("Add a --json flag!") == "add-a-json-flag"
    assert slugify("   ") == "task"
    assert len(slugify("x" * 100)) <= 24


def test_run_id_shape() -> None:
    run_id = new_run_id("Fix the thing")
    assert "fix-the-thing" in run_id
    assert len(run_id.split("-", 2)) == 3


def test_state_roundtrip(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "r1")
    state = RunState(run_id="r1", workflow="feature", task="do it", repo=str(tmp_path))
    state.start_step("plan")
    state.end_step("plan", "ok", "session s1")
    state.build_session_id = "s1"
    state.add_cost(0.25)
    state.add_cost(None)
    save_state(state, run_dir)
    loaded = load_state(run_dir)
    assert loaded.build_session_id == "s1"
    assert loaded.total_cost_usd == 0.25
    assert loaded.step("plan").status == "ok"
    assert (run_dir / "agent").is_dir() and (run_dir / "gates").is_dir()


def test_save_is_atomic_no_stray_tmp(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "r2")
    state = RunState(run_id="r2", workflow="feature", task="t", repo=str(tmp_path))
    for _ in range(3):
        save_state(state, run_dir)
    leftovers = [p for p in run_dir.iterdir() if p.name.startswith(".state-")]
    assert leftovers == []


def test_cancelled_status_and_pid_fields_roundtrip(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "rc")
    state = RunState(run_id="rc", workflow="feature", task="t", repo=str(tmp_path))
    state.status = "cancelled"
    state.pid = 123
    state.pgid = 123
    save_state(state, run_dir)
    loaded = load_state(run_dir)
    assert loaded.status == "cancelled"
    assert loaded.pid == 123
    assert loaded.pgid == 123

    # An older state.json without pid/pgid keys still loads (fields default to None).
    payload = json.loads((run_dir / "state.json").read_text())
    del payload["pid"]
    del payload["pgid"]
    (run_dir / "state.json").write_text(json.dumps(payload))
    reloaded = load_state(run_dir)
    assert reloaded.pid is None
    assert reloaded.pgid is None


def test_list_runs_skips_junk(tmp_path: Path) -> None:
    run_dir = create_run_dir(tmp_path, "r3")
    save_state(RunState(run_id="r3", workflow="feature", task="t", repo=""), run_dir)
    (tmp_path / ".adw" / "runs" / "junk").mkdir()
    runs = list_runs(tmp_path)
    assert [r.run_id for r in runs] == ["r3"]
