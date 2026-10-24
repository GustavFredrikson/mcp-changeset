"""Processes, broken ledgers, clocks, sizes and unicode."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_changeset import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    ChangeError,
    Guard,
    Ledger,
    LedgerError,
    Proposal,
    Status,
)

from _procs import make_counter_guard, race_apply

pytestmark = pytest.mark.anyio
HERE = Path(__file__).resolve().parent


# -- processes ---------------------------------------------------------------


def test_processes_racing_apply_exactly_one_wins(tmp_path):
    ledger, counter = str(tmp_path / "ledger.db"), str(tmp_path / "applied.log")
    preview = asyncio.run(make_counter_guard(ledger, counter).preview("bump", n=1))
    workers = 8
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Manager().Barrier(workers)
    with ctx.Pool(workers) as pool:
        outcomes = pool.starmap(
            race_apply,
            [(ledger, counter, preview["change_id"], preview["confirmation_token"], barrier)]
            * workers,
        )
    assert outcomes.count("ok") == 1, outcomes
    assert all("cannot be applied" in o for o in outcomes if o != "ok")
    assert len(Path(counter).read_text().splitlines()) == 1
    assert Ledger(ledger).get(preview["change_id"]).status is Status.APPLIED


async def test_ledger_locked_by_another_process(tmp_path, store):
    from todo_server import build_server

    ledger = tmp_path / "ledger.db"
    _, guard = build_server(store, ledger, write_mode="guarded", busy_timeout=0.2)
    p = await guard.preview("create_todo", title="x")
    holder = subprocess.Popen(
        [sys.executable, str(HERE / "_procs.py"), str(ledger)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(LedgerError, match="busy"):
            await guard.apply(p["change_id"], p["confirmation_token"])
        assert store.items == {}
    finally:
        holder.communicate("", timeout=10)
    assert guard.status(p["change_id"])["status"] == "pending"  # not burned
    await guard.apply(p["change_id"], p["confirmation_token"])
    assert len(store.items) == 1


# -- broken ledgers ----------------------------------------------------------


def test_corrupt_ledger_file(tmp_path):
    path = tmp_path / "ledger.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(LedgerError, match="corrupt or not a SQLite database"):
        Ledger(path)


def test_ledger_path_is_a_directory(tmp_path):
    (tmp_path / "ledger.db").mkdir()
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "ledger.db")


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_unreadable_ledger_directory(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    Ledger(locked / "ledger.db")
    locked.chmod(0o000)
    try:
        with pytest.raises(LedgerError, match="cannot be"):
            Ledger(locked / "ledger.db")
    finally:
        locked.chmod(0o700)


def test_unsupported_schema_version(tmp_path):
    path = tmp_path / "ledger.db"
    Ledger(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(LedgerError, match="schema version 99"):
        Ledger(path)


def test_existing_ledger_permissions_are_tightened(tmp_path):
    path = tmp_path / "ledger.db"
    Ledger(path)
    path.chmod(0o644)
    Ledger(path)
    assert path.stat().st_mode & 0o777 == 0o600


async def test_corrupted_row_json_is_refused(guard, ledger_path, store):
    p = await guard.preview("create_todo", title="x")
    conn = sqlite3.connect(ledger_path)
    conn.execute(
        "UPDATE changes SET payload_json = '{not json' WHERE change_id = ?", (p["change_id"],)
    )
    conn.commit()
    conn.close()
    with pytest.raises(LedgerError, match="is corrupt"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert store.items == {}


# -- clocks ------------------------------------------------------------------


async def test_clock_moving_backwards_is_refused_without_burning(guard, clock, store):
    p = await guard.preview("create_todo", title="x")
    clock.advance(-120)  # beyond the 60 s default tolerance
    with pytest.raises(ChangeError, match="clock moved backwards"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert guard.status(p["change_id"])["status"] == "pending"
    clock.advance(120)
    await guard.apply(p["change_id"], p["confirmation_token"])
    assert len(store.items) == 1


async def test_small_backward_step_is_tolerated(guard, clock, store):
    p = await guard.preview("create_todo", title="x")
    clock.advance(-30)
    await guard.apply(p["change_id"], p["confirmation_token"])
    assert len(store.items) == 1


async def test_clock_backwards_blocks_undo_too(guard, clock):
    p = await guard.preview("create_todo", title="x")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    clock.advance(-3600)
    with pytest.raises(ChangeError, match="clock moved backwards"):
        await guard.undo(p["change_id"], a["undo_token"])
    assert guard.status(p["change_id"])["status"] == "applied"


async def test_expiry_boundary_is_exclusive(guard, clock):
    p = await guard.preview("create_todo", title="x")
    clock.advance(guard.ttl - 0.001)
    q = await guard.preview("create_todo", title="y")
    await guard.apply(p["change_id"], p["confirmation_token"])  # 1 ms before expiry
    clock.advance(guard.ttl)
    with pytest.raises(ChangeError, match="expired"):
        await guard.apply(q["change_id"], q["confirmation_token"])  # exactly at expiry


async def test_large_forward_jump_expires(guard, clock):
    p = await guard.preview("create_todo", title="x")
    clock.advance(10 * 365 * 86400)
    with pytest.raises(ChangeError, match="expired"):
        await guard.apply(p["change_id"], p["confirmation_token"])


# -- size limits -------------------------------------------------------------


async def test_default_payload_limit(guard):
    assert guard.max_payload_bytes == DEFAULT_MAX_PAYLOAD_BYTES == 65536
    with pytest.raises(ChangeError, match="limit is 65536 bytes"):
        await guard.preview("create_todo", title="x" * 70_000)
    assert guard.ledger.list_changes() == []


async def test_payload_limit_counts_utf8_bytes(make):
    _, guard = make(max_payload_bytes=1000)
    await guard.preview("create_todo", title="x" * 250)  # title appears 3x: ~850 bytes
    with pytest.raises(ChangeError, match="limit is 1000 bytes"):
        await guard.preview("create_todo", title="é" * 250)  # same length, 2 bytes per char


def test_payload_limit_must_be_positive(make):
    with pytest.raises(ValueError, match="max_payload_bytes"):
        make(max_payload_bytes=0)


def test_invalid_write_mode(make):
    with pytest.raises(ValueError, match="write_mode"):
        make(write_mode="yes")


# -- unicode -----------------------------------------------------------------

UNICODE_TITLES = [
    "Köp mjölk 🥛",
    "שלום עולם",  # right-to-left
    "e\u0301 combining",
    "zero\u200bwidth and \u202eoverride",
    "nul\x00byte",
    "汉字 " * 50,
]


@pytest.mark.parametrize("title", UNICODE_TITLES)
async def test_unicode_round_trips_exactly(guard, store, title):
    p = await guard.preview("create_todo", title=title)
    assert p["diff"][1]["after"] == title.strip()
    await guard.apply(p["change_id"], p["confirmation_token"])
    assert next(iter(store.items.values()))["title"] == title.strip()
    assert guard.record(p["change_id"]).payload == {"title": title.strip()}


async def test_lone_surrogate_is_rejected(guard):
    with pytest.raises(ChangeError, match="plain JSON"):
        await guard.preview("create_todo", title="bad \ud800 text")
    assert guard.ledger.list_changes() == []


async def test_nan_is_rejected(make):
    _, guard = make()

    @guard.guarded(apply=lambda payload: None)
    def op() -> Proposal:
        return Proposal(payload={"x": float("nan")})

    with pytest.raises(ChangeError, match="plain JSON"):
        await guard.preview("op")


# -- misc --------------------------------------------------------------------


async def test_preview_must_return_proposal(make):
    _, guard = make()

    @guard.guarded(apply=lambda payload: None)
    def op() -> Proposal:
        return {"payload": 1}  # type: ignore[return-value]

    with pytest.raises(TypeError, match="must return a Proposal"):
        await guard.preview("op")


async def test_unknown_operation(guard):
    with pytest.raises(ChangeError, match="unknown operation"):
        await guard.preview("nope")


async def test_async_callbacks_are_awaited(make):
    _, guard = make()
    state = {"v": 0}

    async def observe(payload):
        return state["v"]

    async def apply(payload):
        state["v"] = payload["v"]
        return {"old": 0}

    async def undo(payload, result):
        state["v"] = result["old"]

    @guard.guarded(apply=apply, undo=undo, observe=observe)
    async def set_v(v: int) -> Proposal:
        return Proposal(payload={"v": v})

    p = await guard.preview("set_v", v=5)
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    assert state["v"] == 5
    await guard.undo(p["change_id"], a["undo_token"])
    assert state["v"] == 0


async def test_undo_failure_is_recorded(make, store):
    _, guard = make()

    def bad_undo(payload, result):
        raise OSError("disk full at /secret/path")

    @guard.guarded(apply=lambda payload: None, undo=bad_undo)
    def op() -> Proposal:
        return Proposal(payload={})

    p = await guard.preview("op")
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    with pytest.raises(ChangeError, match=r"undo of .* failed with OSError") as info:
        await guard.undo(p["change_id"], a["undo_token"])
    assert "/secret/path" not in str(info.value)
    assert guard.status(p["change_id"])["status"] == "undo_failed"
    assert "/secret/path" in (guard.record(p["change_id"]).error or "")


def test_guard_without_shared_tools(tmp_path):
    names: list[str] = []

    class Recorder:
        def add_tool(self, fn, name=None, **_):
            names.append(name)

    guard = Guard(Recorder(), ledger_path=tmp_path / "l.db", shared_tools=False)
    guard.guarded(apply=lambda p: None)(lambda: Proposal(payload={}))
    assert names == ["preview_<lambda>"]


def test_wal_and_shm_files_inherit_private_mode(tmp_path):
    old = os.umask(0o022)
    try:
        path = tmp_path / "ledger.db"
        Ledger(path)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE changes SET status = status")
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            assert side.stat().st_mode & 0o777 == 0o600, suffix
        conn.execute("ROLLBACK")
        conn.close()
    finally:
        os.umask(old)
