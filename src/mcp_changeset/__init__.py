"""Preview -> apply -> undo for state-changing MCP tools.

See https://github.com/GustavFredrikson/mcp-changeset for documentation.
"""

from .errors import ChangeError, LedgerError, StateChangedError, WritesDisabledError
from .guard import DEFAULT_ENV_VAR, DEFAULT_MAX_PAYLOAD_BYTES, Guard, Proposal, diff_fields
from .ledger import ChangeRecord, Ledger, Status, canonical_hash, canonical_json

__all__ = [
    "DEFAULT_ENV_VAR",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "ChangeError",
    "ChangeRecord",
    "Guard",
    "Ledger",
    "LedgerError",
    "Proposal",
    "StateChangedError",
    "Status",
    "WritesDisabledError",
    "canonical_hash",
    "canonical_json",
    "diff_fields",
]

__version__ = "0.1.0"
