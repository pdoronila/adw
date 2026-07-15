from __future__ import annotations

from pathlib import Path

import pytest

from adw.queue.tickets import (
    TicketError,
    claim_next,
    finish,
    list_tickets,
    parse_ticket,
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
