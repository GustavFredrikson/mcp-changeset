"""Real client -> server through the SDK's normal request path.

``server.call_tool()`` goes straight to the tool manager and skips the SDK's
middleware and protocol handling, so these tests drive the server the way a
client does: in memory, and (marker ``stdio``) as a subprocess over stdio.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.anyio

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "todo_server.py"


@asynccontextmanager
async def in_memory(server: Any) -> AsyncIterator[Any]:
    try:  # mcp >= 2
        from mcp import Client
    except ImportError:  # mcp 1.x
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server._mcp_server) as session:
            yield session
    else:
        async with Client(server) as client:
            yield client


@asynccontextmanager
async def over_stdio(state_dir: Path) -> AsyncIterator[Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ, MCP_CHANGESET_WRITE_MODE="guarded", XDG_STATE_HOME=str(state_dir))
    params = StdioServerParameters(command=sys.executable, args=[str(EXAMPLE)], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def is_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", None))


def body(result: Any) -> Any:
    text = result.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def call(client: Any, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    result = await client.call_tool(name, args)
    return is_error(result), body(result)


async def run_flow(client: Any) -> list[dict[str, Any]]:
    """preview -> apply -> replay refused -> undo. Returns list_todos snapshots."""
    snapshots = []

    async def todos() -> dict[str, Any]:
        _, items = await call(client, "list_todos", {})
        snapshots.append(items)
        return items

    names = {t.name for t in (await client.list_tools()).tools}
    assert {"preview_create_todo", "apply_change", "undo_change", "get_change_status"} <= names

    err, preview = await call(client, "preview_create_todo", {"title": "call mom"})
    assert not err and preview["status"] == "preview"
    assert await todos() == {}

    approval = {
        "change_id": preview["change_id"],
        "confirmation_token": preview["confirmation_token"],
    }
    err, applied = await call(client, "apply_change", approval)
    assert not err and applied["status"] == "applied", applied
    assert list((await todos()).values()) == [{"title": "call mom", "done": False}]

    err, replay = await call(client, "apply_change", approval)
    assert err and "cannot be applied (status: applied)" in str(replay)
    assert len(await todos()) == 1

    err, forged = await call(
        client, "apply_change", {**approval, "confirmation_token": "confirm_forged"}
    )
    assert err and "cannot be applied" in str(forged)

    err, status = await call(client, "get_change_status", {"change_id": preview["change_id"]})
    assert not err and status["status"] == "applied"
    dumped = json.dumps(status)
    assert preview["confirmation_token"] not in dumped and applied["undo_token"] not in dumped

    err, undone = await call(
        client,
        "undo_change",
        {"change_id": preview["change_id"], "undo_token": applied["undo_token"]},
    )
    assert not err and undone["status"] == "undone"
    assert await todos() == {}

    err, again = await call(
        client,
        "undo_change",
        {"change_id": preview["change_id"], "undo_token": applied["undo_token"]},
    )
    assert err and "cannot be undone" in str(again)
    return snapshots


async def test_in_memory_client_session(make):
    server, _ = make()
    async with in_memory(server) as client:
        await run_flow(client)


async def test_in_memory_writes_disabled(make):
    server, guard = make(write_mode=None)
    async with in_memory(server) as client:
        err, preview = await call(client, "preview_create_todo", {"title": "x"})
        assert not err and preview["writes_enabled"] is False
        err, refused = await call(
            client,
            "apply_change",
            {
                "change_id": preview["change_id"],
                "confirmation_token": preview["confirmation_token"],
            },
        )
        assert err and "MCP_CHANGESET_WRITE_MODE=guarded" in str(refused)
    assert guard.status(preview["change_id"])["status"] == "pending"


@pytest.mark.stdio
async def test_stdio_subprocess(tmp_path):
    async with over_stdio(tmp_path) as session:
        await run_flow(session)
    ledger = tmp_path / "mcp-changeset-todo" / "ledger.db"
    assert ledger.exists()
    assert ledger.stat().st_mode & 0o777 == 0o600
