"""The file-backed example server, end to end."""

from __future__ import annotations

import os

import pytest

from mcp_changeset import ChangeError, StateChangedError

from notes_server import NoteStore, build_server
from test_client_session import call, in_memory

pytestmark = pytest.mark.anyio


@pytest.fixture
def notes(tmp_path):
    return NoteStore(tmp_path / "notes")


@pytest.fixture
def notes_guard(notes, tmp_path, clock):
    return build_server(notes, tmp_path / "state" / "ledger.db", write_mode="guarded", clock=clock)


async def test_write_replace_delete_and_undo(notes, notes_guard):
    _, guard = notes_guard
    p = await guard.preview("write_note", name="plan", content="v1")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    assert notes.read("plan") == "v1"

    p2 = await guard.preview("write_note", name="plan", content="v2")
    assert p2["diff"] == [{"field": "content", "before": "v1", "after": "v2"}]
    a2 = await guard.apply(p2["change_id"], p2["confirmation_token"])
    assert notes.read("plan") == "v2"

    await guard.undo(p2["change_id"], a2["undo_token"])
    assert notes.read("plan") == "v1"
    await guard.undo(p["change_id"], a["undo_token"])
    assert notes.read("plan") is None

    notes.write("old", "keep me")
    d = await guard.preview("delete_note", name="old")
    da = await guard.apply(d["change_id"], d["confirmation_token"])
    assert notes.names() == []
    await guard.undo(d["change_id"], da["undo_token"])
    assert notes.read("old") == "keep me"


async def test_external_edit_invalidates_approval(notes, notes_guard):
    _, guard = notes_guard
    notes.write("plan", "original")
    p = await guard.preview("write_note", name="plan", content="agent version")
    notes.write("plan", "edited by a human")
    with pytest.raises(StateChangedError):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert notes.read("plan") == "edited by a human"


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", "", ".hidden", "x" * 65, "nul\x00"])
async def test_bad_names_are_rejected(notes_guard, name):
    _, guard = notes_guard
    with pytest.raises(ChangeError, match="note names"):
        await guard.preview("write_note", name=name, content="x")


async def test_symlinks_are_refused(notes, notes_guard, tmp_path):
    _, guard = notes_guard
    target = tmp_path / "outside.md"
    target.write_text("secret")
    os.symlink(target, notes.root / "link.md")
    with pytest.raises(ChangeError, match="symlink"):
        await guard.preview("write_note", name="link", content="overwrite")
    assert target.read_text() == "secret"
    assert notes.names() == []


async def test_limits_and_noops(notes, notes_guard):
    _, guard = notes_guard
    with pytest.raises(ChangeError, match="limited to"):
        await guard.preview("write_note", name="big", content="x" * 20_000)
    notes.write("same", "x")
    with pytest.raises(ChangeError, match="nothing to change"):
        await guard.preview("write_note", name="same", content="x")
    with pytest.raises(ChangeError, match="no note"):
        await guard.preview("delete_note", name="missing")


async def test_over_mcp_with_status(notes, notes_guard):
    server, _ = notes_guard
    async with in_memory(server) as client:
        err, p = await call(client, "preview_write_note", {"name": "todo", "content": "milk"})
        assert not err
        args = {"change_id": p["change_id"], "confirmation_token": p["confirmation_token"]}
        err, a = await call(client, "apply_change", args)
        assert not err and a["status"] == "applied"
        err, content = await call(client, "read_note", {"name": "todo"})
        assert not err and content == "milk"
        err, names = await call(client, "list_notes", {})
        assert not err and "todo" in str(names)
        err, status = await call(client, "get_change_status", {"change_id": p["change_id"]})
        assert not err and status["status"] == "applied" and status["undoable"] is True
        err, u = await call(
            client, "undo_change", {"change_id": p["change_id"], "undo_token": a["undo_token"]}
        )
        assert not err and u["status"] == "undone"
        err, missing = await call(client, "read_note", {"name": "todo"})
        assert err and "no note" in str(missing)
