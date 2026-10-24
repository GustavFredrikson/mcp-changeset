"""Ledger I/O off the event loop, TTL bounds, reconcile and prune."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mcp_changeset import ChangeError, Guard, Ledger, LedgerError, Proposal, Status

from _procs import NullServer

pytestmark = pytest.mark.anyio
HERE = Path(__file__).resolve().parent


# -- ledger I/O in worker threads -------------------------------------------


async def test_ledger_calls_run_off_the_event_loop(guard, monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    for name in ("create", "get", "claim_apply", "finish_apply", "claim_undo", "finish_undo"):
        original = getattr(guard.ledger, name)

        def spy(*args, __name=name, __original=original, **kwargs):
            seen[__name] = threading.get_ident()
            return __original(*args, **kwargs)

        monkeypatch.setattr(guard.ledger, name, spy)
    p = await guard.preview("create_todo", title="x")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    await guard.undo(p["change_id"], a["undo_token"])
    assert set(seen) == {
        "create",
        "get",
        "claim_apply",
        "finish_apply",
        "claim_undo",
        "finish_undo",
    }
    assert loop_thread not in seen.values()


async def test_gathered_applies_in_one_loop_run_once(guard, store):
    p = await guard.preview("create_todo", title="x")
    results = await asyncio.gather(
        *(guard.apply(p["change_id"], p["confirmation_token"]) for _ in range(20)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert all(isinstance(r, ChangeError) for r in results if not isinstance(r, dict))
    assert len(store.items) == 1


async def test_event_loop_keeps_running_while_ledger_is_locked(tmp_path, store):
    from todo_server import build_server

    ledger = tmp_path / "ledger.db"
    _, guard = build_server(store, ledger, write_mode="guarded", busy_timeout=1.0)
    p = await guard.preview("create_todo", title="x")
    holder = subprocess.Popen(
        [sys.executable, str(HERE / "_procs.py"), str(ledger)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        tick_task = asyncio.ensure_future(ticker())
        start = time.monotonic()
        with pytest.raises(LedgerError, match="busy"):
            await guard.apply(p["change_id"], p["confirmation_token"])
        waited = time.monotonic() - start
        tick_task.cancel()
    finally:
        holder.communicate("", timeout=10)
    assert waited >= 0.9
    assert ticks >= 10  # the loop ran during the ~1 s lock wait
    assert guard.status(p["change_id"])["status"] == "pending"


async def test_status_tool_offloads(make):
    server, _ = make()
    from test_client_session import call, in_memory

    async with in_memory(server) as client:
        err, p = await call(client, "preview_create_todo", {"title": "x"})
        assert not err
        err, status = await call(client, "get_change_status", {"change_id": p["change_id"]})
        assert not err and status["status"] == "pending"


# -- TTL bounds ---------------------------------------------------------------


def test_custom_ttl_bounds(tmp_path):
    g = Guard(NullServer(), ledger_path=tmp_path / "l.db", ttl=10, min_ttl=5, max_ttl=20)
    assert g.ttl == 10
    with pytest.raises(ValueError, match="between 5 and 20"):
        Guard(NullServer(), ledger_path=tmp_path / "l.db", ttl=30, min_ttl=5, max_ttl=20)


@pytest.mark.parametrize(("lo", "hi"), [(0, 10), (-1, 10), (20, 10)])
def test_invalid_ttl_bounds(tmp_path, lo, hi):
    with pytest.raises(ValueError, match="ttl bounds"):
        Guard(NullServer(), ledger_path=tmp_path / "l.db", ttl=10, min_ttl=lo, max_ttl=hi)


def test_max_undo_ttl(tmp_path):
    path = tmp_path / "l.db"
    Guard(NullServer(), ledger_path=path, undo_ttl=3600, max_undo_ttl=3600)
    with pytest.raises(ValueError, match="at most max_undo_ttl"):
        Guard(NullServer(), ledger_path=path, undo_ttl=7200, max_undo_ttl=3600)
    with pytest.raises(ValueError, match="at most max_undo_ttl"):
        Guard(NullServer(), ledger_path=path, undo_ttl=None, max_undo_ttl=3600)
    with pytest.raises(ValueError, match="max_undo_ttl must be positive"):
        Guard(NullServer(), ledger_path=path, max_undo_ttl=0)


async def test_short_ttl_with_custom_bounds(tmp_path, clock, store):
    from todo_server import build_server

    _, guard = build_server(
        store, tmp_path / "l.db", write_mode="guarded", clock=clock, ttl=5, min_ttl=1
    )
    p = await guard.preview("create_todo", title="x")
    clock.advance(5)
    with pytest.raises(ChangeError, match="expired"):
        await guard.apply(p["change_id"], p["confirmation_token"])


# -- reconcile ----------------------------------------------------------------


async def _stuck(guard) -> str:
    """A change left in 'applying' by a cancelled apply."""
    started = asyncio.Event()

    async def hang(payload):
        started.set()
        await asyncio.sleep(3600)

    guard.guarded(apply=hang, name="hang")(lambda: Proposal(payload={}))
    p = await guard.preview("hang")
    task = asyncio.ensure_future(guard.apply(p["change_id"], p["confirmation_token"]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return str(p["change_id"])


async def test_reconcile_stuck_apply(guard):
    change_id = await _stuck(guard)
    record = guard.ledger.reconcile(change_id, "checked remote: nothing was written")
    assert record.status is Status.APPLY_FAILED
    assert record.error == "reconciled by operator: checked remote: nothing was written"
    assert guard.status(change_id)["error"] == "interrupted; reconciled by the operator"


async def test_reconcile_stuck_undo(guard, ledger_path):
    import sqlite3

    p = await guard.preview("create_todo", title="x")
    await guard.apply(p["change_id"], p["confirmation_token"])
    conn = sqlite3.connect(ledger_path)
    conn.execute("UPDATE changes SET status = 'undoing' WHERE change_id = ?", (p["change_id"],))
    conn.commit()
    conn.close()
    assert guard.ledger.reconcile(p["change_id"], "restored by hand").status is Status.UNDO_FAILED


async def test_reconcile_refuses_other_states_and_empty_notes(guard):
    p = await guard.preview("create_todo", title="x")
    with pytest.raises(ChangeError, match="only applying or undoing"):
        guard.ledger.reconcile(p["change_id"], "note")
    with pytest.raises(ValueError, match="note is required"):
        guard.ledger.reconcile(p["change_id"], "   ")
    assert guard.status(p["change_id"])["status"] == "pending"


# -- prune --------------------------------------------------------------------


async def _one_of_each(make, clock):
    """Rows in every state; returns {status: change_id}."""
    _, guard = make(undo_ttl=3600)
    ids: dict[str, str] = {}

    async def new(title: str) -> dict:
        return await guard.preview("create_todo", title=title)

    p = await new("pending")
    ids["pending"] = p["change_id"]
    p = await new("expired")
    ids["expired"] = p["change_id"]
    p = await new("applied")
    ids["applied"] = p["change_id"]
    await guard.apply(p["change_id"], p["confirmation_token"])
    p = await new("undone")
    ids["undone"] = p["change_id"]
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    await guard.undo(p["change_id"], a["undo_token"])
    ids["applying"] = await _stuck(guard)
    clock.advance(guard.ttl)
    guard.ledger.expire_stale()  # expires 'pending' and 'expired'
    return guard, ids


async def test_prune_deletes_only_old_finished_rows(make, clock):
    guard, ids = await _one_of_each(make, clock)
    assert guard.ledger.prune(86400) == 0  # nothing is old enough yet
    clock.advance(2 * 86400)
    assert guard.ledger.prune(86400, dry_run=True) == 3  # 2 expired + undone
    assert len(guard.ledger.list_changes()) == 5
    assert guard.ledger.prune(86400) == 3
    left = {r.change_id for r in guard.ledger.list_changes()}
    assert left == {ids["applied"], ids["applying"]}


async def test_prune_include_applied_respects_undo_window(make, clock):
    _, guard = make(undo_ttl=10 * 86400)
    p = await guard.preview("create_todo", title="x")
    await guard.apply(p["change_id"], p["confirmation_token"])
    clock.advance(2 * 86400)
    assert guard.ledger.prune(86400, include_applied=True) == 0  # undo still possible
    clock.advance(10 * 86400)
    assert guard.ledger.prune(86400, include_applied=True) == 1


async def test_prune_keeps_never_expiring_undo(make, clock):
    _, guard = make(undo_ttl=None)
    p = await guard.preview("create_todo", title="x")
    await guard.apply(p["change_id"], p["confirmation_token"])
    clock.advance(365 * 86400)
    assert guard.ledger.prune(0, include_applied=True) == 0


async def test_prune_applied_without_undo(make, clock):
    _, guard = make()
    guard.guarded(apply=lambda payload: None, name="fire")(lambda: Proposal(payload={}))
    p = await guard.preview("fire")
    await guard.apply(p["change_id"], p["confirmation_token"])
    clock.advance(2)
    assert guard.ledger.prune(1) == 0
    assert guard.ledger.prune(1, include_applied=True) == 1


def test_prune_rejects_negative_age(tmp_path):
    with pytest.raises(ValueError, match="older_than"):
        Ledger(tmp_path / "l.db").prune(-1)


# -- opening existing ledgers only -------------------------------------------


def test_create_false_requires_existing_ledger(tmp_path):
    with pytest.raises(LedgerError, match="no ledger file"):
        Ledger(tmp_path / "missing.db", create=False)
    assert not (tmp_path / "missing.db").exists()


def test_create_false_rejects_foreign_database(tmp_path):
    import sqlite3

    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE notes (x)")
    conn.close()
    with pytest.raises(LedgerError, match="not an mcp-changeset ledger"):
        Ledger(other, create=False)
    conn = sqlite3.connect(other)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"notes"}  # untouched


def test_create_false_rejects_symlink(tmp_path):
    real = tmp_path / "real.db"
    Ledger(real)
    (tmp_path / "link.db").symlink_to(real)
    with pytest.raises(LedgerError, match="symlink"):
        Ledger(tmp_path / "link.db", create=False)
