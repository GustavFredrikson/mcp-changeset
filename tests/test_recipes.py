"""Run the python blocks in docs/recipes.md, in order, in one namespace."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver", reason="the recipes use mcp 2.x MCPServer")

RECIPES = Path(__file__).resolve().parents[1] / "docs" / "recipes.md"


def test_recipes_run(capsys, monkeypatch):
    monkeypatch.delenv("MCP_CHANGESET_WRITE_MODE", raising=False)
    blocks = re.findall(r"```python\n(.*?)```", RECIPES.read_text(), re.S)
    assert len(blocks) >= 3
    namespace: dict[str, object] = {"__name__": "recipes"}
    for i, block in enumerate(blocks):
        exec(compile(block, f"{RECIPES.name}[{i}]", "exec"), namespace)  # noqa: S102
    out = capsys.readouterr().out
    assert "refused: change was previewed under a different scope" in out
    assert '"status": "applied"' in out
    assert "rows would be pruned" in out
