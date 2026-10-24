"""Create an MCP server on whichever SDK major version is installed."""

from __future__ import annotations

import importlib
from typing import Any


def new_server(name: str) -> Any:
    """``MCPServer(name)`` on mcp 2.x, ``FastMCP(name)`` on mcp 1.x."""
    try:
        return importlib.import_module("mcp.server.mcpserver").MCPServer(name)
    except ImportError:  # pragma: no cover - mcp 1.x
        return importlib.import_module("mcp.server.fastmcp").FastMCP(name)
