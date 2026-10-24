# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - unreleased

First release.

### Added

- `Guard` and `@guard.guarded(apply=..., undo=..., observe=...)`, registering
  `preview_<op>` plus shared `apply_change`, `undo_change` and
  `get_change_status` tools on FastMCP (mcp 1.x) or MCPServer (mcp 2.x);
  `shared_tools=False` to expose `Guard.apply`/`undo`/`status` through your
  own tools.
- SQLite ledger state machine (`pending → applying → applied → undoing →
  undone`, plus `expired`, `apply_failed`, `undo_failed`) with
  `BEGIN IMMEDIATE` claims: at-most-once apply across threads and processes.
- Single-use, expiring, hashed confirmation and undo tokens bound to the
  change id and payload hash. Undo tokens expire after `undo_ttl` (default
  24 h, `None` for no expiry); the original design note has no undo expiry.
- Optional `observe` hook binding approval to observed state at apply and undo.
- Writes disabled unless `MCP_CHANGESET_WRITE_MODE=guarded` (or
  `write_mode="guarded"`).
- Optional `scope` binding; `0600` ledger file; `max_payload_bytes` limit
  (64 KiB); refusal on large backward clock jumps; `LedgerError` for busy,
  corrupt or unreadable ledgers; sanitized errors for agents.
- Examples: file-backed notes server, todo server, and composition with the
  SDK's MRTR elicitation (mcp 2.x).

[Unreleased]: https://github.com/GustavFredrikson/mcp-changeset/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/GustavFredrikson/mcp-changeset/releases/tag/v0.1.0
