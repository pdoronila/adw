"""File-based ticket queue: directory location IS the state.

<repo>/.adw/tickets/{queue,in_progress,done,failed}/ — transitions are atomic
os.rename calls, which double as the lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

STATES = ("queue", "in_progress", "done", "failed")
DEFAULT_PRIORITY = 5


class TicketError(ValueError):
    pass


@dataclass
class Ticket:
    path: Path
    workflow: str
    title: str
    body: str
    priority: int = DEFAULT_PRIORITY
    repo: Path | None = None
    source_run: str | None = None  # run id that auto-filed this ticket; None for human-created

    @property
    def task(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()


def tickets_root(repo: Path) -> Path:
    return repo / ".adw" / "tickets"


def ensure_dirs(repo: Path) -> Path:
    root = tickets_root(repo)
    for state in STATES:
        (root / state).mkdir(parents=True, exist_ok=True)
    return root


def parse_ticket(path: Path) -> Ticket:
    text = path.read_text()
    if not text.startswith("---"):
        raise TicketError(f"{path}: missing YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise TicketError(f"{path}: malformed frontmatter")
    meta: dict[str, Any] = yaml.safe_load(parts[1]) or {}
    if "title" not in meta:
        raise TicketError(f"{path}: frontmatter must include 'title'")
    return Ticket(
        path=path,
        workflow=str(meta.get("workflow", "feature")),
        title=str(meta["title"]),
        body=parts[2].strip(),
        priority=int(meta.get("priority", DEFAULT_PRIORITY)),
        repo=Path(meta["repo"]).expanduser() if meta.get("repo") else None,
        source_run=str(meta["source_run"]) if meta.get("source_run") else None,
    )


def write_ticket(
    repo: Path,
    title: str,
    body: str,
    workflow: str = "feature",
    priority: int = DEFAULT_PRIORITY,
    target_repo: Path | None = None,
    source_run: str | None = None,
) -> Path:
    ensure_dirs(repo)
    from adw.state.run_state import slugify

    meta: dict[str, Any] = {"workflow": workflow, "title": title, "priority": priority}
    if target_repo is not None:
        meta["repo"] = str(target_repo)
    if source_run is not None:
        meta["source_run"] = source_run
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    queue_dir = tickets_root(repo) / "queue"
    stem = f"{stamp}-{slugify(title)}"
    path = queue_dir / f"{stem}.md"
    # Two failures in the same second slugify to the same stem; disambiguate.
    seq = 2
    while path.exists():
        path = queue_dir / f"{stem}-{seq}.md"
        seq += 1
    frontmatter = yaml.safe_dump(meta, sort_keys=False).strip()
    path.write_text(f"---\n{frontmatter}\n---\n\n{body.strip()}\n")
    return path


def list_tickets(repo: Path, state: str = "queue") -> list[Ticket]:
    directory = tickets_root(repo) / state
    if not directory.is_dir():
        return []
    # Capture the sort key while reading each file — a concurrent claim_next may
    # rename tickets away, so never stat again during the sort.
    scored: list[tuple[int, float, Ticket]] = []
    for path in directory.glob("*.md"):
        try:
            ticket = parse_ticket(path)
            mtime = path.stat().st_mtime
        except (TicketError, OSError):
            continue
        scored.append((ticket.priority, mtime, ticket))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [ticket for _, _, ticket in scored]


def claim_next(repo: Path) -> Ticket | None:
    """Atomically move the highest-priority ticket into in_progress and return it."""
    ensure_dirs(repo)
    for ticket in list_tickets(repo, "queue"):
        target = tickets_root(repo) / "in_progress" / ticket.path.name
        try:
            ticket.path.rename(target)
        except OSError:
            continue
        ticket.path = target
        return ticket
    return None


def finish(ticket: Ticket, repo: Path, outcome: str, detail: str, run_id: str) -> Path:
    """Move an in_progress ticket to done/ or failed/ and append the result."""
    state = "done" if outcome == "shipped" else "failed"
    target = tickets_root(repo) / state / ticket.path.name
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    result_section = (
        f"\n\n## Result\n- outcome: {outcome}\n- run_id: {run_id}\n"
        f"- detail: {detail}\n- at: {stamp}\n"
    )
    with ticket.path.open("a") as handle:
        handle.write(result_section)
    ticket.path.rename(target)
    ticket.path = target
    return target


def find_failed(repo: Path, needle: str) -> Ticket:
    """Locate one failed ticket by exact stem, or unique substring of stem/title."""
    candidates = list_tickets(repo, "failed")
    for ticket in candidates:
        if ticket.path.stem == needle:
            return ticket
    matches = [
        t for t in candidates
        if needle in t.path.stem or needle in t.title
    ]
    if not matches:
        raise TicketError(f"no failed ticket matches {needle!r}")
    if len(matches) > 1:
        names = ", ".join(t.path.stem for t in matches)
        raise TicketError(f"ambiguous match for {needle!r}: {names}")
    return matches[0]


def requeue(repo: Path, ticket: Ticket) -> Path:
    """Move a failed ticket back to queue/, stripping any appended '## Result' section."""
    text = ticket.path.read_text()
    clean_text, _, _ = text.partition("\n\n## Result\n")
    ticket.path.write_text(clean_text if clean_text.endswith("\n") else clean_text + "\n")
    target = tickets_root(repo) / "queue" / ticket.path.name
    ticket.path.rename(target)
    ticket.path = target
    return target
