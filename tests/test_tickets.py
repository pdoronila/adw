from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from adw.cli import app
from adw.queue.tickets import (
    TicketError,
    claim_next,
    find_failed,
    finish,
    list_tickets,
    parse_ticket,
    requeue,
    write_ticket,
)


def test_write_and_parse(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "Add login", "Use JWT.", workflow="feature", priority=2)
    ticket = parse_ticket(path)
    assert ticket.title == "Add login"
    assert ticket.workflow == "feature"
    assert ticket.priority == 2
    assert "Use JWT." in ticket.body
    assert "Add login" in ticket.task


def test_parse_rejects_missing_frontmatter(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("just text")
    with pytest.raises(TicketError):
        parse_ticket(bad)


def test_priority_ordering(tmp_path: Path) -> None:
    write_ticket(tmp_path, "later", "", priority=9)
    write_ticket(tmp_path, "urgent", "", priority=1)
    tickets = list_tickets(tmp_path, "queue")
    assert [t.title for t in tickets] == ["urgent", "later"]


def test_claim_and_finish(tmp_path: Path) -> None:
    write_ticket(tmp_path, "work item", "body", priority=1)
    ticket = claim_next(tmp_path)
    assert ticket is not None
    assert ticket.path.parent.name == "in_progress"
    assert claim_next(tmp_path) is None  # queue drained

    final = finish(ticket, tmp_path, "shipped", "commit abc123", "run-1")
    assert final.parent.name == "done"
    text = final.read_text()
    assert "## Result" in text and "run-1" in text


def test_failed_outcome_goes_to_failed(tmp_path: Path) -> None:
    write_ticket(tmp_path, "doomed", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    final = finish(ticket, tmp_path, "failed", "gates exhausted", "run-2")
    assert final.parent.name == "failed"


def test_ticket_with_target_repo(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "cross repo", "", target_repo=Path("/some/other/repo"))
    ticket = parse_ticket(path)
    assert ticket.repo == Path("/some/other/repo")


def test_source_run_round_trip(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "investigate", "", source_run="run-123")
    assert parse_ticket(path).source_run == "run-123"


def test_source_run_defaults_none(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "plain", "")
    assert parse_ticket(path).source_run is None


def test_same_title_same_second_distinct_paths(tmp_path: Path) -> None:
    first = write_ticket(tmp_path, "same title", "")
    second = write_ticket(tmp_path, "same title", "")
    assert first != second
    assert first.exists() and second.exists()


def test_requeue_cleans_and_moves_ticket(tmp_path: Path) -> None:
    write_ticket(tmp_path, "doomed", "do the thing")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "gates exhausted", "run-3")

    failed = list_tickets(tmp_path, "failed")
    assert len(failed) == 1
    target = requeue(tmp_path, failed[0])

    assert target.parent.name == "queue"
    text = target.read_text()
    assert "## Result" not in text
    reparsed = parse_ticket(target)
    assert reparsed.title == "doomed"
    assert any(t.title == "doomed" for t in list_tickets(tmp_path, "queue"))


def test_find_failed_by_stem(tmp_path: Path) -> None:
    write_ticket(tmp_path, "doomed", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "boom", "run-4")

    failed = list_tickets(tmp_path, "failed")[0]
    found = find_failed(tmp_path, failed.path.stem)
    assert found.path == failed.path


def test_find_failed_by_unique_substring(tmp_path: Path) -> None:
    write_ticket(tmp_path, "fix login bug", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "boom", "run-5")

    found = find_failed(tmp_path, "login")
    assert found.title == "fix login bug"


def test_find_failed_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(TicketError):
        find_failed(tmp_path, "nonexistent")


def test_find_failed_raises_when_ambiguous(tmp_path: Path) -> None:
    for title in ("fix login bug", "fix login page"):
        write_ticket(tmp_path, title, "")
        ticket = claim_next(tmp_path)
        assert ticket is not None
        finish(ticket, tmp_path, "failed", "boom", "run-x")

    with pytest.raises(TicketError):
        find_failed(tmp_path, "login")


runner = CliRunner()


def test_cli_queue_retry_all(tmp_path: Path) -> None:
    for title in ("first", "second"):
        write_ticket(tmp_path, title, "")
        ticket = claim_next(tmp_path)
        assert ticket is not None
        finish(ticket, tmp_path, "failed", "boom", "run-y")

    result = runner.invoke(app, ["queue", "retry", "--all", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert list_tickets(tmp_path, "failed") == []
    assert len(list_tickets(tmp_path, "queue")) == 2


def test_cli_queue_retry_nonexistent_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["queue", "retry", "nonexistent", "--repo", str(tmp_path)])
    assert result.exit_code != 0


def test_cli_queue_retry_requires_exactly_one_arg(tmp_path: Path) -> None:
    result = runner.invoke(app, ["queue", "retry", "--repo", str(tmp_path)])
    assert result.exit_code == 2
