"""Composition with the SDK's MRTR elicitation flow (mcp >= 2 only)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp.server.mcpserver", reason="MRTR helpers need mcp >= 2")

from mcp import Client, types

from elicit_server import build_server

pytestmark = pytest.mark.anyio


def answer(action: str, approve: bool = True):
    asked: list[str] = []

    async def callback(context, params):
        asked.append(params.message)
        content = {"approve": approve} if action == "accept" else None
        return types.ElicitResult(action=action, content=content)

    return callback, asked


async def preview(client):
    r = await client.call_tool("preview_set_setting", {"key": "theme", "value": "dark"})
    p = json.loads(r.content[0].text)
    return {"change_id": p["change_id"], "confirmation_token": p["confirmation_token"]}


@pytest.fixture
def server(tmp_path, clock):
    return build_server(str(tmp_path / "ledger.db"), write_mode="guarded", clock=clock)


async def test_user_approval_applies_once(server):
    srv, _, settings = server
    callback, asked = answer("accept")
    async with Client(srv, elicitation_callback=callback) as client:
        args = await preview(client)
        r = await client.call_tool("apply_change", args)
        assert not r.is_error, r.content
        assert settings == {"theme": "dark"}
        assert asked == [
            'Apply this change? Set theme: [{"field": "theme", "before": "light", "after": "dark"}]'
        ]

        replay = await client.call_tool("apply_change", args)
        assert replay.is_error and "not pending" in replay.content[0].text
        assert len(asked) == 1  # the user is not asked about a spent change


@pytest.mark.parametrize("action,approve", [("decline", True), ("cancel", True), ("accept", False)])
async def test_user_refusal_leaves_change_pending(server, action, approve):
    srv, guard, settings = server
    callback, asked = answer(action, approve)
    async with Client(srv, elicitation_callback=callback) as client:
        args = await preview(client)
        r = await client.call_tool("apply_change", args)
        assert r.is_error
    assert settings == {"theme": "light"}
    # decline/cancel abort before the tool body; "approve: false" reaches the body,
    # which refuses before calling apply. Either way the token is not consumed.
    assert guard.status(args["change_id"])["status"] == "pending"
    assert len(asked) == 1


async def test_writes_disabled_does_not_ask(tmp_path, clock):
    srv, _, _ = build_server(str(tmp_path / "l.db"), write_mode="disabled", clock=clock)
    callback, asked = answer("accept")
    async with Client(srv, elicitation_callback=callback) as client:
        args = await preview(client)
        r = await client.call_tool("apply_change", args)
        assert r.is_error and "disabled" in r.content[0].text
    assert asked == []
