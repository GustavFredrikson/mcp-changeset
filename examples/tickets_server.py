"""A SQLite-backed ticket tracker with conditional writes.

The notes and todo examples bind approvals with ``observe`` only, which leaves
a small window between the re-check and the write. This one closes it the way
SECURITY.md recommends: every ticket row has a ``version`` that each write
bumps, ``observe`` returns that version, and ``apply``/``undo`` use
``UPDATE ... WHERE version = ?``. If someone edits the ticket in between, the
write matches no row and the change fails closed with ``StateChangedError``.

    TICKETS_DB=~/tickets.db MCP_CHANGESET_WRITE_MODE=guarded python examples/tickets_server.py

The ticket database is a stand-in for any remote system that supports
compare-and-set (an ETag/If-Match header, a row version, a revision id).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from mcp_changeset import ChangeError, Guard, Proposal, StateChangedError, diff_fields

from _compat import new_server

TicketStatus = Literal["open", "in_progress", "closed"]
FIELDS = ("title", "status", "assignee")
MAX_TITLE = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title    TEXT NOT NULL,
    status   TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'closed')),
    assignee TEXT,
    version  INTEGER NOT NULL DEFAULT 1
);
"""


class TicketDB:
    """Tickets in a SQLite file. Every write bumps ``version``."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get(self, ticket_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    def all(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM tickets ORDER BY id")]

    def create(self, title: str, status: str = "open", assignee: str | None = None) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO tickets (title, status, assignee) VALUES (?, ?, ?)",
                (title, status, assignee),
            )
            assert cur.lastrowid is not None  # noqa: S101 - set by every INSERT
            return cur.lastrowid

    def update_if(self, ticket_id: int, version: int, fields: dict[str, Any]) -> int:
        """Compare-and-set: write ``fields`` only if the row is still at ``version``.

        Returns the new version. Raises :class:`StateChangedError` otherwise.
        """
        unknown = set(fields) - set(FIELDS)
        if unknown or not fields:
            raise ChangeError(f"cannot update fields {sorted(unknown) or '(none)'}")
        # Column names come from FIELDS above, never from the agent.
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE tickets SET {sets}, version = version + 1 "  # noqa: S608
                "WHERE id = ? AND version = ?",
                (*fields.values(), ticket_id, version),
            )
        if cur.rowcount != 1:
            raise StateChangedError(f"ticket {ticket_id} changed concurrently; nothing written")
        return version + 1

    def delete_if(self, ticket_id: int, version: int) -> None:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM tickets WHERE id = ? AND version = ?", (ticket_id, version)
            )
        if cur.rowcount != 1:
            raise StateChangedError(f"ticket {ticket_id} changed concurrently; nothing deleted")


def build_server(db: TicketDB, ledger_path: str | Path, **guard_kwargs: Any) -> tuple[Any, Guard]:
    """Build the tickets server and its :class:`Guard`."""
    server = new_server("tickets")
    guard = Guard(server, ledger_path=ledger_path, **guard_kwargs)

    def list_tickets() -> list[dict[str, Any]]:
        """List all tickets with their current version."""
        return db.all()

    server.add_tool(list_tickets)

    def observe(payload: dict[str, Any]) -> dict[str, Any] | None:
        ticket_id = payload.get("ticket_id")
        if ticket_id is None:  # create: nothing to observe yet
            return None
        ticket = db.get(ticket_id)
        return None if ticket is None else {"version": ticket["version"]}

    # -- create -------------------------------------------------------------

    def apply_create(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ticket_id": db.create(payload["title"], assignee=payload["assignee"])}

    def undo_create(payload: dict[str, Any], result: dict[str, Any]) -> None:
        # Only delete it if nobody has touched it since (still version 1).
        db.delete_if(result["ticket_id"], 1)

    @guard.guarded(apply=apply_create, undo=undo_create)
    def create_ticket(title: str, assignee: str | None = None) -> Proposal:
        """Open a new ticket."""
        title = title.strip()
        if not title or len(title) > MAX_TITLE:
            raise ChangeError(f"title must be 1-{MAX_TITLE} characters")
        after = {"title": title, "status": "open", "assignee": assignee}
        return Proposal(
            payload={"title": title, "assignee": assignee},
            diff=diff_fields(None, after),
            summary=f"Create ticket {title!r}",
        )

    # -- update -------------------------------------------------------------

    def apply_update(payload: dict[str, Any]) -> dict[str, Any]:
        new_version = db.update_if(payload["ticket_id"], payload["version"], payload["fields"])
        return {"version": new_version}

    def undo_update(payload: dict[str, Any], result: dict[str, Any]) -> None:
        # Restore the previewed "before" values, but only over our own write.
        db.update_if(payload["ticket_id"], result["version"], payload["before"])

    @guard.guarded(apply=apply_update, undo=undo_update, observe=observe)
    def update_ticket(
        ticket_id: int,
        status: TicketStatus | None = None,
        assignee: str | None = None,
        title: str | None = None,
    ) -> Proposal:
        """Change a ticket's status, assignee or title."""
        ticket = db.get(ticket_id)
        if ticket is None:
            raise ChangeError(f"no ticket {ticket_id}")
        if title is not None and not 0 < len(title.strip()) <= MAX_TITLE:
            raise ChangeError(f"title must be 1-{MAX_TITLE} characters")
        wanted = {"status": status, "assignee": assignee, "title": title}
        fields = {k: v for k, v in wanted.items() if v is not None and ticket[k] != v}
        if not fields:
            raise ChangeError("nothing to change")
        before = {k: ticket[k] for k in fields}
        return Proposal(
            # The version the human saw is part of what they approve.
            payload={
                "ticket_id": ticket_id,
                "version": ticket["version"],
                "fields": fields,
                "before": before,
            },
            diff=diff_fields(before, fields),
            summary=f"Update ticket {ticket_id} ({ticket['title']!r})",
        )

    return server, guard


if __name__ == "__main__":
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    tickets = TicketDB(os.environ.get("TICKETS_DB", state / "mcp-changeset-tickets" / "tickets.db"))
    server, _ = build_server(tickets, state / "mcp-changeset-tickets" / "ledger.db")
    server.run()
