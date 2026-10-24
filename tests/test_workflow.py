from __future__ import annotations

import sqlite3
import stat

import pytest

from mcp_changeset import ChangeError, Guard, Proposal, StateChangedError, Status

pytestmark = pytest.mark.anyio


async def test_create_apply_undo(guard, store):
    preview = await guard.preview("create_todo", title="  buy milk ")
    assert preview["status"] == "preview"
    assert preview["diff"] == [
        {"field": "done", "before": None, "after": False},
        {"field": "title", "before": None, "after": "buy milk"},
    ]
    assert store.items == {}  # preview writes nothing

    applied = await guard.apply(preview["change_id"], preview["confirmation_token"])
    assert applied["status"] == "applied"
    assert store.items == {1: {"title": "buy milk", "done": False}}
    assert guard.status(preview["change_id"])["status"] == "applied"

    undone = await guard.undo(preview["change_id"], applied["undo_token"])
    assert undone["status"] == "undone"
    assert store.items == {}


async def test_update_undo_restores_only_touched_fields(guard, store):
    todo_id = store.create("draft")
    p = await guard.preview("update_todo", todo_id=todo_id, done=True)
    assert p["diff"] == [{"field": "done", "before": False, "after": True}]
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    assert store.get(todo_id) == {"title": "draft", "done": True}
    await guard.undo(p["change_id"], a["undo_token"])
    assert store.get(todo_id) == {"title": "draft", "done": False}


async def test_delete_undo_recreates(guard, store):
    todo_id = store.create("old")
    p = await guard.preview("delete_todo", todo_id=todo_id)
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    assert store.items == {}
    await guard.undo(p["change_id"], a["undo_token"])
    assert list(store.items.values()) == [{"title": "old", "done": False}]


async def test_preview_rejection_stores_nothing(guard):
    with pytest.raises(ChangeError, match="must not be empty"):
        await guard.preview("create_todo", title="   ")
    assert guard.ledger.list_changes() == []


# -- tokens -----------------------------------------------------------------


async def test_token_expires(guard, store, clock):
    p = await guard.preview("create_todo", title="x")
    clock.advance(guard.ttl)
    with pytest.raises(ChangeError, match="expired"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert guard.status(p["change_id"])["status"] == "expired"
    with pytest.raises(ChangeError, match=r"cannot be applied \(status: expired\)"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert store.items == {}


async def test_token_valid_just_before_expiry(guard, store, clock):
    p = await guard.preview("create_todo", title="x")
    clock.advance(guard.ttl - 1)
    await guard.apply(p["change_id"], p["confirmation_token"])
    assert len(store.items) == 1


async def test_expire_stale(guard, clock):
    await guard.preview("create_todo", title="a")
    await guard.preview("create_todo", title="b")
    clock.advance(guard.ttl)
    assert guard.ledger.expire_stale() == 2
    assert {r.status for r in guard.ledger.list_changes()} == {Status.EXPIRED}


async def test_token_is_single_use(guard, store):
    p = await guard.preview("create_todo", title="x")
    await guard.apply(p["change_id"], p["confirmation_token"])
    with pytest.raises(ChangeError, match=r"cannot be applied \(status: applied\)"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert len(store.items) == 1


async def test_wrong_token_does_not_burn_preview(guard, store):
    p = await guard.preview("create_todo", title="x")
    with pytest.raises(ChangeError, match="invalid confirmation token"):
        await guard.apply(p["change_id"], "confirm_guess")
    assert guard.status(p["change_id"])["status"] == "pending"
    await guard.apply(p["change_id"], p["confirmation_token"])
    assert len(store.items) == 1


async def test_token_of_one_change_does_not_apply_another(guard, store):
    a = await guard.preview("create_todo", title="a")
    b = await guard.preview("create_todo", title="b")
    with pytest.raises(ChangeError, match="invalid confirmation token"):
        await guard.apply(b["change_id"], a["confirmation_token"])
    assert store.items == {}


async def test_failed_apply_consumes_token(make, store, ledger_path, clock):
    calls = []
    _, guard = make()

    def boom(payload):
        calls.append(payload)
        raise RuntimeError("remote said 500")

    @guard.guarded(apply=boom)
    def flaky(x: int) -> Proposal:
        return Proposal(payload={"x": x})

    p = await guard.preview("flaky", x=1)
    with pytest.raises(ChangeError, match="failed with RuntimeError") as info:
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert "remote said 500" not in str(info.value)  # internals stay server-side
    status = guard.status(p["change_id"])
    assert status["status"] == "apply_failed"
    assert "remote said 500" not in status["error"]
    assert guard.record(p["change_id"]).error == "RuntimeError: remote said 500"
    with pytest.raises(ChangeError, match=r"cannot be applied \(status: apply_failed\)"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert calls == [{"x": 1}]  # at most once, even on retry


async def test_replay_after_restart(make, store):
    _, guard = make()
    p = await guard.preview("create_todo", title="x")
    await guard.apply(p["change_id"], p["confirmation_token"])
    _, restarted = make()  # same ledger file, fresh process state
    with pytest.raises(ChangeError, match="cannot be applied"):
        await restarted.apply(p["change_id"], p["confirmation_token"])
    assert len(store.items) == 1


async def test_tokens_are_not_stored_or_exposed(guard, ledger_path):
    p = await guard.preview("create_todo", title="x")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    raw = b"".join(f.read_bytes() for f in ledger_path.parent.iterdir())
    for token in (p["confirmation_token"], a["undo_token"]):
        assert token.encode() not in raw
        assert token not in repr(guard.status(p["change_id"]))
        assert token not in repr(guard.record(p["change_id"]))


# -- payload binding --------------------------------------------------------


def _tamper(ledger_path, change_id, sql, *args):
    conn = sqlite3.connect(ledger_path)
    conn.execute(sql, (*args, change_id))
    conn.commit()
    conn.close()


async def test_tampered_payload_is_refused(guard, store, ledger_path):
    p = await guard.preview("create_todo", title="harmless")
    _tamper(
        ledger_path,
        p["change_id"],
        "UPDATE changes SET payload_json = ? WHERE change_id = ?",
        '{"title":"evil"}',
    )
    with pytest.raises(ChangeError, match="does not match its hash"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert store.items == {}


async def test_tampered_payload_and_hash_invalidates_token(guard, store, ledger_path):
    from mcp_changeset import canonical_hash

    p = await guard.preview("create_todo", title="harmless")
    evil = {"title": "evil"}
    _tamper(
        ledger_path,
        p["change_id"],
        "UPDATE changes SET payload_json = ?, payload_hash = ? WHERE change_id = ?",
        '{"title":"evil"}',
        canonical_hash(evil),
    )
    with pytest.raises(ChangeError, match="invalid confirmation token"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert store.items == {}


async def test_apply_runs_the_stored_normalized_payload(make, store):
    seen = []
    _, guard = make()

    @guard.guarded(apply=lambda payload: seen.append(payload))
    def op(when: str) -> Proposal:
        return Proposal(payload={"when": when.upper(), "n": (1, 2)})

    p = await guard.preview("op", when="fri")
    await guard.apply(p["change_id"], p["confirmation_token"])
    assert seen == [{"when": "FRI", "n": [1, 2]}]


async def test_non_json_payload_is_rejected(make):
    _, guard = make()

    @guard.guarded(apply=lambda payload: None)
    def op() -> Proposal:
        return Proposal(payload={"x": object()})

    with pytest.raises(ChangeError, match="plain JSON"):
        await guard.preview("op")


# -- state binding ----------------------------------------------------------


async def test_state_changed_after_preview_fails_closed(guard, store):
    todo_id = store.create("Tempo 3x10")
    p = await guard.preview("update_todo", todo_id=todo_id, done=True)
    store.update(todo_id, {"title": "Easy run"})  # edited elsewhere
    with pytest.raises(StateChangedError, match="fresh preview"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert store.get(todo_id) == {"title": "Easy run", "done": False}
    assert guard.status(p["change_id"])["status"] == "apply_failed"


async def test_undo_refuses_to_overwrite_later_edit(guard, store):
    todo_id = store.create("a")
    p = await guard.preview("update_todo", todo_id=todo_id, title="b")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    store.update(todo_id, {"title": "c"})
    with pytest.raises(StateChangedError, match="refusing to overwrite"):
        await guard.undo(p["change_id"], a["undo_token"])
    assert store.get(todo_id)["title"] == "c"
    assert guard.status(p["change_id"])["status"] == "undo_failed"


# -- undo tokens ------------------------------------------------------------


async def test_undo_is_single_use(guard, store):
    p = await guard.preview("create_todo", title="x")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    await guard.undo(p["change_id"], a["undo_token"])
    with pytest.raises(ChangeError, match=r"cannot be undone \(status: undone\)"):
        await guard.undo(p["change_id"], a["undo_token"])


async def test_undo_needs_undo_token_not_confirmation_token(guard):
    p = await guard.preview("create_todo", title="x")
    await guard.apply(p["change_id"], p["confirmation_token"])
    with pytest.raises(ChangeError, match="invalid undo token"):
        await guard.undo(p["change_id"], p["confirmation_token"])
    assert guard.status(p["change_id"])["status"] == "applied"


async def test_undo_window_expires(guard, clock):
    p = await guard.preview("create_todo", title="x")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    clock.advance(guard.undo_ttl)
    with pytest.raises(ChangeError, match="undo window"):
        await guard.undo(p["change_id"], a["undo_token"])


async def test_undo_ttl_none_never_expires(make, clock):
    _, guard = make(undo_ttl=None)
    p = await guard.preview("create_todo", title="x")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    assert a["undo_expires_at"] is None
    clock.advance(365 * 86400)
    await guard.undo(p["change_id"], a["undo_token"])


async def test_undo_ttl_is_configurable(make, clock):
    _, guard = make(undo_ttl=120)
    p = await guard.preview("create_todo", title="x")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    clock.advance(120)
    with pytest.raises(ChangeError, match="undo window"):
        await guard.undo(p["change_id"], a["undo_token"])


def test_undo_ttl_must_be_positive(make):
    with pytest.raises(ValueError, match="undo_ttl"):
        make(undo_ttl=0)


async def test_operation_without_undo(make):
    _, guard = make()

    @guard.guarded(apply=lambda payload: {"ok": True})
    def fire(x: int) -> Proposal:
        return Proposal(payload={"x": x})

    p = await guard.preview("fire", x=1)
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    assert "undo_token" not in a
    assert guard.status(p["change_id"])["undoable"] is False
    with pytest.raises(ChangeError, match="no undo"):
        await guard.undo(p["change_id"], "undo_x")


# -- write mode -------------------------------------------------------------


async def test_writes_disabled_by_default(make, store, monkeypatch):
    _, guard = make(write_mode=None)
    p = await guard.preview("create_todo", title="x")
    assert p["writes_enabled"] is False
    from mcp_changeset import WritesDisabledError

    with pytest.raises(WritesDisabledError, match="MCP_CHANGESET_WRITE_MODE=guarded"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert guard.status(p["change_id"])["status"] == "pending"  # not burned
    assert store.items == {}

    monkeypatch.setenv("MCP_CHANGESET_WRITE_MODE", "guarded")
    a = await guard.apply(p["change_id"], p["confirmation_token"])

    monkeypatch.setenv("MCP_CHANGESET_WRITE_MODE", "off")
    with pytest.raises(WritesDisabledError):
        await guard.undo(p["change_id"], a["undo_token"])
    assert guard.status(p["change_id"])["status"] == "applied"


async def test_explicit_disabled_overrides_env(make, monkeypatch):
    monkeypatch.setenv("MCP_CHANGESET_WRITE_MODE", "guarded")
    _, guard = make(write_mode="disabled")
    assert guard.writes_enabled is False


async def test_custom_env_var(make, monkeypatch):
    monkeypatch.setenv("TODO_WRITE_MODE", "GUARDED")
    _, guard = make(write_mode=None, env_var="TODO_WRITE_MODE")
    assert guard.writes_enabled is True


# -- scope, config, files ---------------------------------------------------


async def test_scope_mismatch_is_refused(make, store):
    _, alice = make(scope="alice")
    p = await alice.preview("create_todo", title="x")
    _, bob = make(scope="bob")
    with pytest.raises(ChangeError, match="different scope"):
        await bob.apply(p["change_id"], p["confirmation_token"])
    assert store.items == {}
    await alice.apply(p["change_id"], p["confirmation_token"])  # wrong scope did not burn it


@pytest.mark.parametrize("ttl", [0, 59, 3601])
def test_ttl_bounds(make, ttl):
    with pytest.raises(ValueError, match="ttl"):
        make(ttl=ttl)


def test_ledger_file_permissions(guard, ledger_path):
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger_path.parent.stat().st_mode) == 0o700


def test_unknown_change(guard):
    with pytest.raises(ChangeError, match="unknown change"):
        guard.status("chg_nope")


def test_duplicate_operation_rejected(make):
    _, guard = make()
    with pytest.raises(ValueError, match="already registered"):
        guard.guarded(apply=lambda p: None, name="create_todo")(lambda: Proposal(payload={}))


def test_guard_accepts_any_server_with_add_tool(tmp_path):
    class Recorder:
        def __init__(self):
            self.names = []

        def add_tool(self, fn, name=None, **_):
            self.names.append(name)

    srv = Recorder()
    g = Guard(srv, ledger_path=tmp_path / "l.db", prefix="cal_")

    @g.guarded(apply=lambda p: None)
    def move_event() -> Proposal:
        return Proposal(payload={})

    assert srv.names == [
        "cal_apply_change",
        "cal_undo_change",
        "cal_get_change_status",
        "cal_preview_move_event",
    ]


async def test_pending_preview_survives_restart(make, store):
    _, guard = make()
    p = await guard.preview("create_todo", title="x")
    _, restarted = make()  # new process state, same ledger file
    await restarted.apply(p["change_id"], p["confirmation_token"])
    assert len(store.items) == 1
