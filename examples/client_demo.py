"""Drive an example server the way an MCP client does: over stdio.

Starts ``notes_server.py`` as a subprocess with a throwaway notes directory and
ledger, then walks through preview -> apply -> replay (refused) -> undo and
prints each step. Nothing outside a temporary directory is touched.

    python examples/client_demo.py          # or: uv run examples/client_demo.py

It works with mcp 1.x and 2.x and is run in CI as a smoke test.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).resolve().with_name("notes_server.py")


def _parse(result: Any) -> tuple[bool, Any]:
    is_error = bool(getattr(result, "is_error", None) or getattr(result, "isError", None))
    if not result.content:  # e.g. an empty list
        structured = getattr(result, "structured_content", None) or getattr(
            result, "structuredContent", None
        )
        return is_error, (structured or {}).get("result", [])
    text = result.content[0].text
    try:
        return is_error, json.loads(text)
    except json.JSONDecodeError:
        return is_error, text


async def demo(workdir: Path, out: Any = sys.stdout) -> list[tuple[str, bool, Any]]:
    """Run the walkthrough against a fresh server in ``workdir``.

    Returns ``(tool, is_error, body)`` for every call, for tests.
    """
    env = dict(
        os.environ,
        MCP_CHANGESET_WRITE_MODE="guarded",
        NOTES_DIR=str(workdir / "notes"),
        XDG_STATE_HOME=str(workdir / "state"),
    )
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    steps: list[tuple[str, bool, Any]] = []

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(tool: str, **args: Any) -> Any:
            is_error, body = _parse(await session.call_tool(tool, args))
            steps.append((tool, is_error, body))
            shown = {k: v for k, v in args.items() if not k.endswith("token")}
            print(f"\n> {tool}({json.dumps(shown)})", file=out)
            print(("ERROR: " if is_error else "") + json.dumps(body, indent=2), file=out)
            return body

        tools = sorted(t.name for t in (await session.list_tools()).tools)
        print("tools:", ", ".join(tools), file=out)

        preview = await call("preview_write_note", name="hello", content="Hello from an agent")
        approval = {
            "change_id": preview["change_id"],
            "confirmation_token": preview["confirmation_token"],
        }
        await call("list_notes")  # still empty: a preview changes nothing
        applied = await call("apply_change", **approval)
        await call("read_note", name="hello")
        await call("apply_change", **approval)  # replay: refused, the token is spent
        await call("undo_change", change_id=preview["change_id"], undo_token=applied["undo_token"])
        await call("list_notes")
        await call("get_change_status", change_id=preview["change_id"])
    return steps


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-changeset-demo-") as tmp:
        steps = anyio.run(demo, Path(tmp))
    expected = [False, False, False, False, True, False, False, False]
    if [err for _, err, _ in steps] != expected:
        sys.exit("demo did not behave as expected")
    print("\nOK: preview -> apply -> replay refused -> undo")


if __name__ == "__main__":
    main()
