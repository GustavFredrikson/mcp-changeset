"""End-to-end through the MCP server's own tool dispatch (mcp 1.x and 2.x)."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.anyio


async def call(server: Any, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    """Return (is_error, parsed payload) across SDK versions."""
    try:
        result = await server.call_tool(name, args)
    except Exception as exc:  # mcp 1.x raises ToolError
        return True, str(exc)
    if isinstance(result, tuple):  # mcp 1.x: (content, structured)
        content = result[0]
        is_error = False
    elif hasattr(result, "content"):  # mcp 2.x: CallToolResult
        content = result.content
        is_error = bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
    else:
        content, is_error = result, False
    text = content[0].text
    try:
        return is_error, json.loads(text)
    except json.JSONDecodeError:
        return is_error, text


async def tool_names(server: Any) -> set[str]:
    return {t.name for t in await server.list_tools()}


async def test_only_previews_and_shared_tools_are_exposed(make):
    server, _ = make()
    assert await tool_names(server) == {
        "list_todos",
        "preview_create_todo",
        "preview_update_todo",
        "preview_delete_todo",
        "apply_change",
        "undo_change",
        "get_change_status",
    }


async def test_apply_tool_takes_no_change_data(make):
    server, _ = make()
    tools = {t.name: t for t in await server.list_tools()}
    apply_tool = tools["apply_change"]
    schema = getattr(apply_tool, "input_schema", None) or apply_tool.inputSchema
    assert set(schema["properties"]) == {"change_id", "confirmation_token"}
    annotations = tools["apply_change"].annotations
    destructive = getattr(annotations, "destructive_hint", None)
    if destructive is None:
        destructive = annotations.destructiveHint
    assert destructive is True


async def test_full_flow_over_mcp(make, store):
    server, _ = make()
    err, preview = await call(server, "preview_create_todo", {"title": "call mom"})
    assert not err and preview["status"] == "preview"
    assert store.items == {}

    err, applied = await call(
        server,
        "apply_change",
        {"change_id": preview["change_id"], "confirmation_token": preview["confirmation_token"]},
    )
    assert not err and applied["status"] == "applied"
    assert list(store.items.values()) == [{"title": "call mom", "done": False}]

    err, replay = await call(
        server,
        "apply_change",
        {"change_id": preview["change_id"], "confirmation_token": preview["confirmation_token"]},
    )
    assert err and "cannot be applied" in str(replay)

    err, status = await call(server, "get_change_status", {"change_id": preview["change_id"]})
    assert not err and status["status"] == "applied"
    assert "confirm_" not in json.dumps(status)

    err, undone = await call(
        server,
        "undo_change",
        {"change_id": preview["change_id"], "undo_token": applied["undo_token"]},
    )
    assert not err and undone["status"] == "undone"
    assert store.items == {}
