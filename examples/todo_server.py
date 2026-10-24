"""A toy todo server whose writes go through preview -> apply -> undo.

Run it (writes enabled) with:

    MCP_CHANGESET_WRITE_MODE=guarded python examples/todo_server.py

Without the environment variable the agent can still preview changes, but
apply_change and undo_change refuse to run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp_changeset import ChangeError, Guard, Proposal, diff_fields

from _compat import new_server


class TodoStore:
    """Stands in for a remote API. Ids are never reused."""

    def __init__(self) -> None:
        self.items: dict[int, dict[str, Any]] = {}
        self._next = 1

    def get(self, todo_id: int) -> dict[str, Any] | None:
        item = self.items.get(todo_id)
        return dict(item) if item else None

    def create(self, title: str, done: bool = False) -> int:
        todo_id, self._next = self._next, self._next + 1
        self.items[todo_id] = {"title": title, "done": done}
        return todo_id

    def update(self, todo_id: int, fields: dict[str, Any]) -> None:
        self.items[todo_id].update(fields)

    def delete(self, todo_id: int) -> None:
        del self.items[todo_id]


def build_server(
    store: TodoStore, ledger_path: str | Path, **guard_kwargs: Any
) -> tuple[Any, Guard]:
    server = new_server("todo")
    guard = Guard(server, ledger_path=ledger_path, **guard_kwargs)

    def list_todos() -> dict[str, Any]:
        """List all todos."""
        return {str(k): v for k, v in store.items.items()}

    server.add_tool(list_todos)

    # -- create ---------------------------------------------------------

    def apply_create(payload: dict[str, Any]) -> dict[str, Any]:
        return {"todo_id": store.create(payload["title"])}

    def undo_create(payload: dict[str, Any], result: dict[str, Any]) -> None:
        store.delete(result["todo_id"])

    @guard.guarded(apply=apply_create, undo=undo_create)
    def create_todo(title: str) -> Proposal:
        """Add a todo."""
        title = title.strip()
        if not title:
            raise ChangeError("title must not be empty")
        return Proposal(
            payload={"title": title},
            diff=diff_fields(None, {"title": title, "done": False}),
            summary=f"Create todo {title!r}",
        )

    # -- update ---------------------------------------------------------

    def observe(payload: dict[str, Any]) -> dict[str, Any] | None:
        return store.get(payload["todo_id"])

    def apply_update(payload: dict[str, Any]) -> dict[str, Any]:
        before = store.get(payload["todo_id"])
        if before is None:
            raise ChangeError("todo disappeared")
        store.update(payload["todo_id"], payload["fields"])
        # Only the touched fields are restored on undo.
        return {"previous": {k: before[k] for k in payload["fields"]}}

    def undo_update(payload: dict[str, Any], result: dict[str, Any]) -> None:
        store.update(payload["todo_id"], result["previous"])

    @guard.guarded(apply=apply_update, undo=undo_update, observe=observe)
    def update_todo(todo_id: int, title: str | None = None, done: bool | None = None) -> Proposal:
        """Rename a todo or mark it done / not done."""
        before = store.get(todo_id)
        if before is None:
            raise ChangeError(f"no todo {todo_id}")
        fields = {k: v for k, v in {"title": title, "done": done}.items() if v is not None}
        changed = {k: v for k, v in fields.items() if before[k] != v}
        if not changed:
            raise ChangeError("nothing to change")
        return Proposal(
            payload={"todo_id": todo_id, "fields": changed},
            diff=diff_fields({k: before[k] for k in changed}, changed),
            summary=f"Update todo {todo_id}",
        )

    # -- delete ---------------------------------------------------------

    def apply_delete(payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = store.get(payload["todo_id"])
        store.delete(payload["todo_id"])
        return {"snapshot": snapshot}

    def undo_delete(payload: dict[str, Any], result: dict[str, Any]) -> None:
        # Compensation, not rollback: the todo comes back with a new id.
        store.create(**result["snapshot"])

    @guard.guarded(apply=apply_delete, undo=undo_delete, observe=observe)
    def delete_todo(todo_id: int) -> Proposal:
        """Delete a todo."""
        before = store.get(todo_id)
        if before is None:
            raise ChangeError(f"no todo {todo_id}")
        return Proposal(
            payload={"todo_id": todo_id},
            diff=diff_fields(before, None),
            summary=f"Delete todo {todo_id} ({before['title']!r})",
        )

    return server, guard


if __name__ == "__main__":
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    server, _ = build_server(TodoStore(), state / "mcp-changeset-todo" / "ledger.db")
    server.run()
