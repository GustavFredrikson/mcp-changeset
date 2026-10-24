"""Composing mcp-changeset with the SDK's own confirmation flow (mcp >= 2 only).

MCP 2026-07-28 lets a tool ask the *user* mid-call (Multi Round-Trip
Requests). The Python SDK's ``Resolve``/``Elicit`` helpers do the asking, and
its ``RequestStateBoundary`` seals the round-trip state and binds it to the
call's arguments. mcp-changeset adds what that does not: a stored payload, a
single-use ledger, a state check and undo.

Here the agent still needs the confirmation token, and the user must also
accept the diff in their client before anything runs.
"""

# No `from __future__ import annotations`: the SDK evaluates the Resolve(...)
# annotation below, and it refers to a local function.
import json
from typing import Annotated, Any

from mcp.server.mcpserver import Elicit, MCPServer, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from mcp_changeset import ChangeError, Guard, Proposal, WritesDisabledError, diff_fields


class Approval(BaseModel):
    approve: bool


def build_server(ledger_path: str, **guard_kwargs: Any) -> tuple[MCPServer, Guard, dict[str, str]]:
    """A one-setting server whose apply asks the user to approve the stored diff."""
    server = MCPServer("settings")
    guard = Guard(server, ledger_path=ledger_path, shared_tools=False, **guard_kwargs)
    settings = {"theme": "light"}

    def apply(payload: dict[str, Any]) -> dict[str, Any]:
        previous = settings[payload["key"]]
        settings[payload["key"]] = payload["value"]
        return {"previous": previous}

    def undo(payload: dict[str, Any], result: dict[str, Any]) -> None:
        settings[payload["key"]] = result["previous"]

    @guard.guarded(apply=apply, undo=undo, observe=lambda p: settings.get(p["key"]))
    def set_setting(key: str, value: str) -> Proposal:
        """Change a setting."""
        if key not in settings:
            raise ChangeError(f"unknown setting {key!r}")
        return Proposal(
            payload={"key": key, "value": value},
            diff=diff_fields({key: settings[key]}, {key: value}),
            summary=f"Set {key}",
        )

    def ask_user(change_id: str) -> Elicit[Approval]:
        # Don't ask the user about something that cannot run anyway.
        if not guard.writes_enabled:
            raise WritesDisabledError("writes are disabled on this server")
        change = guard.status(change_id)
        if change["status"] != "pending":
            raise ChangeError(f"change {change_id} is {change['status']}, not pending")
        # Rendered from the ledger, not from anything the agent sent. JSON with
        # ensure_ascii keeps newlines, bidi and zero-width characters escaped.
        diff = json.dumps(change["diff"], ensure_ascii=True)
        return Elicit(f"Apply this change? {change['summary']}: {diff}", Approval)

    @server.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    async def apply_change(
        change_id: str,
        confirmation_token: str,
        approval: Annotated[Approval, Resolve(ask_user)],
    ) -> dict[str, Any]:
        """Apply a previewed change after the user approves it in their client."""
        if not approval.approve:
            raise ChangeError("the user did not approve this change")
        return await guard.apply(change_id, confirmation_token)

    @server.tool()
    def get_change_status(change_id: str) -> dict[str, Any]:
        """Report the state of a change. Never returns tokens."""
        return guard.status(change_id)

    return server, guard, settings
