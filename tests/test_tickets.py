from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from adw.cli import app
from adw.queue.tickets import (
    TicketError,
    claim_next,
    find_failed,
    find_ticket,
    finish,
    list_tickets,
    parse_ticket,
    remove,
    requeue,
    set_priority,
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


def test_find_ticket_by_stem_across_states(tmp_path: Path) -> None:
    write_ticket(tmp_path, "shipped work", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "done", "run-6")

    done = list_tickets(tmp_path, "done")[0]
    found = find_ticket(tmp_path, done.path.stem)
    assert found.path == done.path


def test_find_ticket_scopes_to_states(tmp_path: Path) -> None:
    write_ticket(tmp_path, "queued only", "")
    queued = list_tickets(tmp_path, "queue")[0]
    with pytest.raises(TicketError):
        find_ticket(tmp_path, queued.path.stem, ("failed",))


def test_find_ticket_ambiguous_across_states(tmp_path: Path) -> None:
    write_ticket(tmp_path, "fix login bug", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "boom", "run-7")

    write_ticket(tmp_path, "fix login page", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "ok", "run-8")

    with pytest.raises(TicketError):
        find_ticket(tmp_path, "login")


def test_remove_deletes_file(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "delete me", "")
    ticket = parse_ticket(path)
    remove(ticket)
    assert not path.exists()


def test_set_priority_rewrites_frontmatter(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "bump me", "the body", workflow="bug", priority=5)
    ticket = parse_ticket(path)
    set_priority(ticket, 1)
    assert ticket.priority == 1

    reparsed = parse_ticket(path)
    assert reparsed.priority == 1
    assert reparsed.title == "bump me"
    assert reparsed.workflow == "bug"
    assert "the body" in reparsed.body


def test_set_priority_preserves_repo_key_and_result_section(tmp_path: Path) -> None:
    write_ticket(tmp_path, "cross repo", "", target_repo=Path("/some/other/repo"))
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "failed", "boom", "run-9")

    failed = list_tickets(tmp_path, "failed")[0]
    set_priority(failed, 1)

    reparsed = parse_ticket(failed.path)
    assert reparsed.priority == 1
    assert reparsed.repo == Path("/some/other/repo")
    assert "## Result" in failed.path.read_text()


def test_requeue_from_done(tmp_path: Path) -> None:
    write_ticket(tmp_path, "shipped work", "do the thing")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "commit abc", "run-10")

    done = list_tickets(tmp_path, "done")[0]
    target = requeue(tmp_path, done)
    assert target.parent.name == "queue"
    assert "## Result" not in target.read_text()
    assert any(t.title == "shipped work" for t in list_tickets(tmp_path, "queue"))


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


def test_cli_ticket_rm_with_yes(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "delete me", "")
    result = runner.invoke(app, ["ticket", "rm", path.stem, "-y", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not path.exists()


def test_cli_ticket_rm_prompts(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "keep me", "")
    result = runner.invoke(
        app, ["ticket", "rm", path.stem, "--repo", str(tmp_path)], input="n\n"
    )
    assert result.exit_code != 0
    assert path.exists()


def test_cli_ticket_bump(tmp_path: Path) -> None:
    path = write_ticket(tmp_path, "bump me", "", priority=5)
    result = runner.invoke(
        app, ["ticket", "bump", path.stem, "--priority", "1", "--repo", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert parse_ticket(path).priority == 1


def test_cli_ticket_requeue_from_done(tmp_path: Path) -> None:
    write_ticket(tmp_path, "shipped work", "")
    ticket = claim_next(tmp_path)
    assert ticket is not None
    finish(ticket, tmp_path, "shipped", "ok", "run-11")

    done = list_tickets(tmp_path, "done")[0]
    result = runner.invoke(app, ["ticket", "requeue", done.path.stem, "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert list_tickets(tmp_path, "done") == []
    assert any(t.title == "shipped work" for t in list_tickets(tmp_path, "queue"))


def test_cli_ticket_edit_invokes_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_ticket(tmp_path, "edit me", "")
    captured: list[list[str]] = []

    def fake(argv: list[str]) -> None:
        captured.append(argv)

    monkeypatch.setattr("adw.cli.subprocess.run", fake)
    monkeypatch.setenv("EDITOR", "myeditor")
    result = runner.invoke(app, ["ticket", "edit", path.stem, "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert captured == [["myeditor", str(path)]]


def test_cli_ticket_rm_ambiguous_exits_nonzero(tmp_path: Path) -> None:
    write_ticket(tmp_path, "fix login bug", "")
    write_ticket(tmp_path, "fix login page", "")
    result = runner.invoke(app, ["ticket", "rm", "login", "-y", "--repo", str(tmp_path)])
    assert result.exit_code != 0
