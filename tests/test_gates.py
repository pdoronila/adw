from __future__ import annotations

from pathlib import Path

from adw.config import GateConfig
from adw.nodes.code_node import run_gate, run_gates, truncate_middle


def test_passing_gate(tmp_path: Path) -> None:
    result = run_gate("lint", GateConfig(command="echo clean"), tmp_path, tmp_path / "logs", 1)
    assert result.ok
    assert result.exit_code == 0
    assert "clean" in result.output_excerpt
    assert result.log_path.is_file()


def test_failing_gate_captures_stderr(tmp_path: Path) -> None:
    result = run_gate(
        "test",
        GateConfig(command="echo out; echo err >&2; exit 3"),
        tmp_path,
        tmp_path / "logs",
        2,
    )
    assert not result.ok
    assert result.exit_code == 3
    assert "err" in result.output_excerpt
    assert "attempt-2-test.log" == result.log_path.name


def test_timeout(tmp_path: Path) -> None:
    result = run_gate("slow", GateConfig(command="sleep 5", timeout=1), tmp_path, tmp_path / "l", 1)
    assert not result.ok
    assert "timed out" in result.output_excerpt


def test_run_gates_no_fail_fast(tmp_path: Path) -> None:
    gates = {
        "a": GateConfig(command="exit 1"),
        "b": GateConfig(command="echo b-ran"),
    }
    results = run_gates(["a", "b"], gates, tmp_path, tmp_path / "logs", 1)
    assert [r.ok for r in results] == [False, True]  # b ran despite a failing


def test_truncate_keeps_head_and_tail() -> None:
    text = "H" * 3000 + "M" * 50000 + "T" * 9000
    excerpt = truncate_middle(text)
    assert excerpt.startswith("H" * 100)
    assert excerpt.endswith("T" * 100)
    assert "truncated" in excerpt
    assert len(excerpt) < 12000


def test_truncate_short_passthrough() -> None:
    assert truncate_middle("short") == "short"
