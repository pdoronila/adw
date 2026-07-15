"""End-to-end smoke test: real CLI, real git, real gates, fake agent binary."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adw.cli import app

FIXTURES = Path(__file__).parent / "fixtures"

runner = CliRunner()


@pytest.fixture()
def e2e_repo(target_repo: Path) -> Path:
    fake = FIXTURES / "fake_agent.py"
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (target_repo / "adw.yaml").write_text(
        f"""
gates:
  marker: {{command: "test -f marker.txt", timeout: 10}}
agents:
  default: {{backend: claude-code, model: sonnet}}
workflow:
  max_fix_iterations: 2
  gate_order: [marker]
backends:
  claude-code: {{binary: "{fake}"}}
"""
    )
    subprocess.run(["git", "add", "-A"], cwd=target_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add adw.yaml"], cwd=target_repo, check=True, capture_output=True
    )
    return target_repo


def test_full_run_ships(e2e_repo: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "feature", "add hello feature", "--repo", str(e2e_repo), "-y"],
        env={**os.environ, "ADW_ASSUME_YES": "1"},
    )
    assert result.exit_code == 0, result.output
    assert "shipped" in result.output

    # the fake agent's work landed in a commit on the adw/ branch
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=e2e_repo, capture_output=True, text=True
    ).stdout.strip()
    assert branch.startswith("adw/")
    assert (e2e_repo / "feature.txt").read_text() == "hello\n"
    assert (e2e_repo / "marker.txt").exists()
    committed = subprocess.run(
        ["git", "ls-files"], cwd=e2e_repo, capture_output=True, text=True
    ).stdout
    assert "feature.txt" in committed and "marker.txt" in committed
    assert ".adw" not in committed

    # artifacts: plan, review, transcripts, gate logs, final state
    run_dirs = list((e2e_repo / ".adw" / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert (run_dir / "plan.md").is_file()
    assert (run_dir / "review.md").read_text().startswith("VERDICT: ship")
    transcripts = sorted(p.name for p in (run_dir / "agent").iterdir())
    assert [t.split("-", 1)[1] for t in transcripts] == [
        "plan.json",
        "build.json",
        "fix-1.json",
        "review.json",
    ]
    assert (run_dir / "gates" / "attempt-1-marker.log").is_file()
    assert '"status": "shipped"' in (run_dir / "state.json").read_text()

    # status command sees the run
    status = runner.invoke(app, ["status", "--repo", str(e2e_repo)])
    assert "shipped" in status.output


def test_ticket_queue_end_to_end(e2e_repo: Path) -> None:
    created = runner.invoke(
        app,
        [
            "ticket",
            "new",
            "add hello via ticket",
            "--repo",
            str(e2e_repo),
            "--body",
            "Create feature.txt and marker.txt.",
            "--priority",
            "1",
        ],
    )
    assert created.exit_code == 0, created.output

    listed = runner.invoke(app, ["queue", "list", "--repo", str(e2e_repo)])
    assert "add hello via ticket" in listed.output

    processed = runner.invoke(
        app,
        ["queue", "process", "--repo", str(e2e_repo), "-y"],
        env={**os.environ, "ADW_ASSUME_YES": "1"},
    )
    assert processed.exit_code == 0, processed.output
    assert "shipped" in processed.output

    done = list((e2e_repo / ".adw" / "tickets" / "done").glob("*.md"))
    assert len(done) == 1
    text = done[0].read_text()
    assert "## Result" in text and "outcome: shipped" in text
    assert not list((e2e_repo / ".adw" / "tickets" / "queue").glob("*.md"))


def test_dry_run_touches_nothing(e2e_repo: Path) -> None:
    result = runner.invoke(
        app, ["run", "feature", "anything", "--repo", str(e2e_repo), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "plan" in result.output and "marker" in result.output
    assert not (e2e_repo / ".adw" / "runs").exists()
