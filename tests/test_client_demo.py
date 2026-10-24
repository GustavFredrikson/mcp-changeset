"""examples/client_demo.py over real stdio, and the client config example."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from client_demo import demo

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.stdio
@pytest.mark.anyio
async def test_client_demo_walkthrough(tmp_path: Path) -> None:
    out = io.StringIO()
    steps = await demo(tmp_path, out)
    assert [(tool, err) for tool, err, _ in steps] == [
        ("preview_write_note", False),
        ("list_notes", False),
        ("apply_change", False),
        ("read_note", False),
        ("apply_change", True),
        ("undo_change", False),
        ("list_notes", False),
        ("get_change_status", False),
    ]
    assert steps[3][2] == "Hello from an agent"
    assert "status: applied" in str(steps[4][2])
    assert steps[-1][2]["status"] == "undone"
    assert not (tmp_path / "notes" / "hello.md").exists()
    printed = out.getvalue()
    # The demo prints results, which include tokens, but never echoes them as arguments.
    assert '"confirmation_token": "confirm_' not in printed.split("> apply_change")[1]


def test_claude_desktop_config_points_at_real_examples() -> None:
    config = json.loads((EXAMPLES / "clients" / "claude_desktop_config.json").read_text())
    servers = config["mcpServers"]
    assert set(servers) == {"changeset-notes", "changeset-tickets"}
    for entry in servers.values():
        assert entry["command"] == "uv"
        args = entry["args"]
        assert args[0] == "--directory" and args[2] == "run"
        assert (EXAMPLES.parent / args[3]).is_file()
        assert entry["env"]["MCP_CHANGESET_WRITE_MODE"] == "guarded"
