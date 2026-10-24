"""Exceptions raised by mcp-changeset.

All of them derive from :class:`ChangeError`, which subclasses the MCP SDK's
``ToolError``. The SDK forwards ``ToolError`` messages to the client, so every
message here is written to be safe to show an agent: no tokens, no paths, no
remote error text.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING


def _tool_error_base() -> type[Exception]:
    for module in ("mcp.server.mcpserver.exceptions", "mcp.server.fastmcp.exceptions"):
        try:
            base: type[Exception] = importlib.import_module(module).ToolError
        except (ImportError, AttributeError):
            continue
        return base
    return Exception  # pragma: no cover - every supported mcp version has one


if TYPE_CHECKING:
    _ToolError = Exception
else:
    _ToolError = _tool_error_base()


class ChangeError(_ToolError):
    """A guarded change was refused or failed."""


class WritesDisabledError(ChangeError):
    """``apply`` or ``undo`` was called while guarded write mode is off."""


class StateChangedError(ChangeError):
    """The observed state no longer matches what was previewed or applied."""


class LedgerError(ChangeError):
    """The ledger database is unavailable, locked, corrupt or unsupported."""
