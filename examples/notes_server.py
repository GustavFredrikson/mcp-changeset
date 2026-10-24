"""A file-backed notes server: writes and deletes go through preview -> apply -> undo.

Notes are plain files in one directory. Reads are ordinary tools; writes and
deletes are guarded, bound to the file's observed content, and undoable.

    NOTES_DIR=~/notes MCP_CHANGESET_WRITE_MODE=guarded python examples/notes_server.py
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from mcp_changeset import ChangeError, Guard, Proposal, diff_fields

from _compat import new_server

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_NOTE_BYTES = 16 * 1024


class NoteStore:
    """Notes as ``<root>/<name>.md``. Names are validated, symlinks are refused."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        if not NAME_RE.fullmatch(name):
            raise ChangeError("note names are 1-64 characters: letters, digits, '-' and '_'")
        path = self.root / f"{name}.md"
        if path.is_symlink():
            raise ChangeError(f"note {name!r} is a symlink; refusing")
        return path

    def read(self, name: str) -> str | None:
        path = self.path(name)
        return path.read_text("utf-8") if path.exists() else None

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.md") if not p.is_symlink())

    def write(self, name: str, content: str) -> None:
        path = self.path(name)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, path)  # atomic on POSIX
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self, name: str) -> None:
        self.path(name).unlink()

    def snapshot(self, name: str) -> dict[str, Any]:
        """What an approval is bound to: existence and a content hash."""
        content = self.read(name)
        if content is None:
            return {"exists": False}
        return {"exists": True, "sha256": hashlib.sha256(content.encode()).hexdigest()}


def build_server(
    store: NoteStore, ledger_path: str | Path, **guard_kwargs: Any
) -> tuple[Any, Guard]:
    """Build the notes server and its :class:`Guard`."""
    server = new_server("notes")
    guard = Guard(server, ledger_path=ledger_path, **guard_kwargs)

    def list_notes() -> list[str]:
        """List note names."""
        return store.names()

    def read_note(name: str) -> str:
        """Read a note."""
        content = store.read(name)
        if content is None:
            raise ChangeError(f"no note {name!r}")
        return content

    server.add_tool(list_notes)
    server.add_tool(read_note)

    def observe(payload: dict[str, Any]) -> dict[str, Any]:
        return store.snapshot(payload["name"])

    # -- write (create or replace) -----------------------------------------

    def apply_write(payload: dict[str, Any]) -> dict[str, Any]:
        previous = store.read(payload["name"])
        store.write(payload["name"], payload["content"])
        return {"previous": previous}

    def undo_write(payload: dict[str, Any], result: dict[str, Any]) -> None:
        if result["previous"] is None:
            store.delete(payload["name"])
        else:
            store.write(payload["name"], result["previous"])

    @guard.guarded(apply=apply_write, undo=undo_write, observe=observe)
    def write_note(name: str, content: str) -> Proposal:
        """Create a note or replace its content."""
        if len(content.encode()) > MAX_NOTE_BYTES:
            raise ChangeError(f"notes are limited to {MAX_NOTE_BYTES} bytes")
        before = store.read(name)
        if before == content:
            raise ChangeError("nothing to change")
        verb = "Create" if before is None else "Replace"
        return Proposal(
            payload={"name": name, "content": content},
            diff=diff_fields(None if before is None else {"content": before}, {"content": content}),
            summary=f"{verb} note {name!r} ({len(content)} characters)",
        )

    # -- delete --------------------------------------------------------------

    def apply_delete(payload: dict[str, Any]) -> dict[str, Any]:
        previous = store.read(payload["name"])
        store.delete(payload["name"])
        return {"previous": previous}

    def undo_delete(payload: dict[str, Any], result: dict[str, Any]) -> None:
        store.write(payload["name"], result["previous"])

    @guard.guarded(apply=apply_delete, undo=undo_delete, observe=observe)
    def delete_note(name: str) -> Proposal:
        """Delete a note."""
        before = store.read(name)
        if before is None:
            raise ChangeError(f"no note {name!r}")
        return Proposal(
            payload={"name": name},
            diff=diff_fields({"content": before}, None),
            summary=f"Delete note {name!r}",
        )

    return server, guard


if __name__ == "__main__":
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    notes = NoteStore(os.environ.get("NOTES_DIR", Path.home() / "notes"))
    server, _ = build_server(notes, state / "mcp-changeset-notes" / "ledger.db")
    server.run()
